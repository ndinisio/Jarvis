"""Skills: recipes that run an errand's usual steps without a model.

A tiny fake shop stands behind the real page tools (browse_to,
read_page_manifest, click_page_element): what's tested is everything the
skill machinery decides — which skill fits, what it asks for, how each step
finds its element, when it stops and hands over, what it learns, and that
it never mistakes a request to buy for anything else.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jarvis.capabilities.automation import AutomationCapability
from jarvis.capabilities.base import Request
from jarvis.intelligence.operator import Budget, Operator
from jarvis.intelligence.schema import Complexity, Objective
from jarvis.skills import SkillError, SkillLibrary, SkillRunner, grounding
from jarvis.skills.learning import learn, proof_phrases
from jarvis.skills.library import wants_to_buy
from jarvis.skills.model import load, render
from jarvis.tools.base import ToolResult


# ---------------------------------------------------------------------------
# a fake shop behind the real page tools
# ---------------------------------------------------------------------------
class Shop:
    """Pages as JARVIS lists them; clicks move between them."""

    def __init__(self, *, product_has_button: bool = True):
        self.page = "blank"
        self.basket: list[str] = []
        self.urls: list[str] = []
        self.product_has_button = product_has_button

    def listing(self) -> str:
        if self.page == "search":
            return ("Page: Amazon.co.uk : aa batteries — https://www.amazon.co.uk/s?k=aa+batteries\n"
                    '[jv1] link "Basket 0" → www.amazon.co.uk/gp/cart/view.html\n'
                    '[jv2] link "Premium Phone Case — Shockproof" → www.amazon.co.uk/dp/B0SPONCASE\n'
                    '[jv3] link "Duracell Plus AA Batteries, Pack of 24" → www.amazon.co.uk/dp/B0AABAT24A\n'
                    '[jv4] link "Duracell Plus AAA Batteries, Pack of 12" → www.amazon.co.uk/dp/B0AAABAT12\n'
                    'Page text: 4 results for "aa batteries"')
        if self.page == "product":
            button = '\n[jv9] button "Add to Basket"' if self.product_has_button else ""
            return ("Page: Duracell Plus AA Batteries — https://www.amazon.co.uk/dp/B0AABAT24A\n"
                    '[jv8] select "Quantity" options=1|2|3' + button + '\n[jv10] button "Buy Now"')
        if self.page == "added":
            return ("Page: Duracell Plus AA Batteries — https://www.amazon.co.uk/dp/B0AABAT24A\n"
                    "Open dialog: Added to Basket\n"
                    '[jv11] link "Go to basket" → www.amazon.co.uk/gp/cart/view.html')
        return "Page: about:blank"

    def install(self, app, monkeypatch) -> None:
        registry = app.deps.registry

        def stub(name, run):
            tool = registry.get(name)
            monkeypatch.setattr(tool, "run", run)
            monkeypatch.setattr(tool.spec, "requires_macos", False)

        async def browse(args, ctx):
            self.urls.append(args.get("url", ""))
            self.page = "search" if "/s?k=" in args.get("url", "") else "blank"
            return ToolResult(data={"url": args.get("url")}, summary=f"Opened {args.get('url')}.")

        async def read(args, ctx):
            return ToolResult(data={"url": "https://www.amazon.co.uk/"}, summary="Listed the page.",
                              observation=self.listing())

        async def click(args, ctx):
            handle = args["handle"]
            if self.page == "search" and handle == "jv3":
                self.page = "product"
                return ToolResult(data={"clicked": True}, summary="Clicked “Duracell Plus AA Batteries”.")
            if self.page == "product" and handle == "jv9":
                self.basket.append("B0AABAT24A")
                self.page = "added"
                return ToolResult(data={"clicked": True}, summary="Clicked “Add to Basket”.")
            return ToolResult.failure(f"Nothing happened clicking {handle}.")

        stub("browse_to", browse)
        stub("read_page_manifest", read)
        stub("click_page_element", click)
        # The consequence check inspects a page target through the browser;
        # with no browser here it judges the label, which is what we want.
        for name in ("click_page_element",):
            async def no_inspect(args, ctx):
                return None
            monkeypatch.setattr(registry.get(name), "inspect", no_inspect)


@pytest.fixture
def shop(app, monkeypatch):
    app.config_store.update({"security": {"confirmation_timeout_s": 0.2}})
    shop = Shop()
    shop.install(app, monkeypatch)
    return shop


def _batteries() -> Objective:
    return Objective(goal="add a pack of AA batteries to my Amazon basket", site="amazon.co.uk",
                     targets=["AA batteries"], complexity=Complexity.MULTI_STEP)


# ---------------------------------------------------------------------------
# the format
# ---------------------------------------------------------------------------
def test_every_built_in_skill_loads_and_has_a_way_to_trigger():
    library = SkillLibrary()
    skills = library.skills()
    assert len(skills) >= 15
    for skill in skills:
        assert skill.sites or skill.apps, f"{skill.id} has no place it applies"
        assert skill.tool_name.startswith("skill_") and len(skill.tool_name) <= 64


@pytest.mark.parametrize(("data", "problem"), [
    ({"id": "x", "title": "X", "steps": [{"teleport": "home"}]}, "unknown action"),
    ({"id": "x", "title": "X", "steps": [{"go": "a", "click": "b"}]}, "exactly one action"),
    ({"id": "x", "title": "X", "steps": []}, "needs steps"),
])
def test_a_malformed_skill_is_refused(data, problem):
    with pytest.raises(SkillError, match=problem):
        load(data)


def test_a_learned_skill_may_not_call_tools_directly():
    with pytest.raises(SkillError, match="only built-in"):
        load({"id": "x", "title": "X", "steps": [{"tool": "run_shell_command"}]}, source="learned")


def test_slots_are_filled_and_url_encoded():
    assert render("https://x/s?k={query|url}", {"query": "AA batteries"}) == "https://x/s?k=AA+batteries"
    assert render({"with": "{query}"}, {"query": "tea"}) == {"with": "tea"}
    with pytest.raises(SkillError):
        render("{missing}", {})


# ---------------------------------------------------------------------------
# which skill fits
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("goal", "targets", "expected"), [
    ("add AA batteries to my Amazon basket", ["AA batteries"], "amazon-add-to-basket"),
    ("pop some batteries in my amazon trolley", ["batteries"], "amazon-add-to-basket"),
    ("what's in my amazon basket", [], "amazon-open-basket"),
    ("find me a usb c cable on amazon", ["usb c cable"], "amazon-search"),
    ("where's my amazon order", [], "amazon-orders"),
    ("get directions to the airport", ["the airport"], "maps-directions"),
    ("open bluetooth settings", [], "settings-bluetooth"),
    ("play despacito on youtube", ["despacito"], "youtube-play"),
    ("what's the best way to learn python", ["python"], None),
    ("check out my amazon basket", [], None),
    ("order the noise cancelling earbuds on amazon", ["earbuds"], None),
    ("buy the earbuds on amazon right now", ["earbuds"], None),
    ("remove the kettle from my amazon basket", ["kettle"], None),
    # Only the purchase rule stops these: "show" fits the search skill, which
    # would open results and call the purchase done.
    ("show me the earbuds on amazon and buy them", ["earbuds"], None),
    ("look up the kettle on amazon and pay for it", ["kettle"], None),
    # Only the skill's exclusions stop this: opening the basket isn't removing.
    ("open my amazon basket and remove the kettle", [], None),
])
def test_the_right_skill_fits_or_none_does(goal, targets, expected):
    found = SkillLibrary().direct(Objective(goal=goal, targets=targets))
    assert (found[0].id if found else None) == expected


def test_a_request_to_buy_never_matches_a_skill_but_a_warning_not_to_does_not_count():
    assert wants_to_buy("buy the earbuds") and wants_to_buy("i want to order some batteries")
    assert not wants_to_buy("add the earbuds to my basket, don't buy them yet")
    assert not wants_to_buy("where's my amazon order")


def test_parameters_come_from_what_was_understood():
    library = SkillLibrary()
    skill = library.get("amazon-add-to-basket")
    us = library.parameters(skill, Objective(goal="add tea to my amazon.com cart", site="amazon.com",
                                             targets=["tea"]))
    assert us == {"query": "tea", "domain": "www.amazon.com"}
    uk = library.parameters(skill, Objective(goal="add tea to my basket", targets=["tea"]))
    assert uk["domain"] == "www.amazon.co.uk", "the default when no site was named"
    assert library.parameters(skill, Objective(goal="add something to my basket")) is None
    assert skill.parameter_schema()["required"] == ["query"], "defaults aren't asked for"


# ---------------------------------------------------------------------------
# grounding
# ---------------------------------------------------------------------------
LISTING = ('[jv1] link "Basket 0" → www.amazon.co.uk/gp/cart/view.html\n'
           '[jv3] link "Duracell Plus AA Batteries, Pack of 24" → www.amazon.co.uk/dp/B0AABAT24A\n'
           '[jv4] link "Duracell Plus AAA Batteries" → www.amazon.co.uk/dp/B0AAABAT12\n'
           '[jv9] button "Add to Basket"\n[ax5] search field "Search" placeholder="Search"')


def test_an_element_is_found_by_what_it_is():
    assert grounding.find(LISTING, text=["Add to Cart", "Add to Basket"], role="button").handle == "jv9"
    assert grounding.find(LISTING, role="link", href="/dp/", best_match="AA batteries").handle == "jv3"
    assert grounding.find(LISTING, fillable=True).handle == "ax5", "roles may be two words"
    assert grounding.find(LISTING, role="link", href="/dp/", best_match="garden hose") is None
    assert grounding.element(LISTING, "[jv4]").text == "Duracell Plus AAA Batteries"
    assert grounding.mentions("Open dialog: Added to Basket", ["Added to Cart", "added to basket"])


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------
async def test_a_skill_runs_the_errand_with_no_model_at_all(app, shop, fake_provider):
    asked: list = []
    fake_provider.router = lambda messages, kwargs: asked.append(messages) or None
    library = SkillLibrary()
    skill = library.get("amazon-add-to-basket")
    outcome = await SkillRunner(app.deps, app.deps.tool_context()).run(
        skill, library.parameters(skill, _batteries()))
    assert outcome.ok and shop.basket == ["B0AABAT24A"]
    assert shop.urls == ["https://www.amazon.co.uk/s?k=AA+batteries"]
    assert outcome.evidence == "Added to Basket"
    assert asked == [], "no model was asked anything"


async def test_a_skill_stops_where_the_page_stops_fitting_and_says_how_far_it_got(app, shop):
    shop.product_has_button = False            # this one needs a size first
    library = SkillLibrary()
    skill = library.get("amazon-add-to-basket")
    outcome = await SkillRunner(app.deps, app.deps.tool_context()).run(
        skill, library.parameters(skill, _batteries()))
    assert not outcome.ok and shop.basket == []
    assert "no button “Add to Basket / Add to Cart”" in outcome.reason
    assert 'clicked link "Duracell Plus AA Batteries, Pack of 24"' in outcome.account()
    assert '[jv8] select "Quantity"' in outcome.view, "whoever carries on sees the page"


async def test_a_skill_whose_proof_never_shows_is_not_done(app, shop):
    skill = load({"id": "search-then-hope", "title": "Search", "sites": ["amazon"],
                  "steps": [{"go": "https://www.amazon.co.uk/s?k={query|url}"}],
                  "done_when": ["Added to Basket"], "params": {"query": {"from": "target"}}})
    outcome = await SkillRunner(app.deps, app.deps.tool_context()).run(skill, {"query": "AA batteries"})
    assert not outcome.ok and "nothing showed “Added to Basket”" in outcome.reason


async def test_an_errand_a_skill_covers_is_done_without_the_operator(app, shop, fake_provider):
    operator_prompts: list = []

    def reply(messages, kwargs):
        if "You operate this Mac" in "\n".join(m.content for m in messages):
            operator_prompts.append(1)
        return None

    fake_provider.router = reply
    task = app.deps.tasks.create("automation", "test")
    response = await AutomationCapability(app.deps).handle(Request(
        text="add a pack of AA batteries to my amazon basket", args={"objective": _batteries()},
        ctx=app.deps.tool_context(task=task), task=task))
    assert response.text == "I've added AA batteries to your Amazon basket."
    assert response.data["skill"] == "amazon-add-to-basket" and response.data["model_calls"] == 0
    assert operator_prompts == [] and shop.basket == ["B0AABAT24A"]


async def test_when_a_skill_stops_the_operator_carries_on_from_the_page(app, shop, fake_provider):
    shop.product_has_button = False
    prompts: list[str] = []

    def reply(messages, kwargs):
        text = "\n".join(m.content for m in messages)
        if "You operate this Mac" in text:
            prompts.append(text)
            return json.dumps({"tool": "give_up", "arguments": {"reason": "it needs a size"}})
        return None

    fake_provider.router = reply
    task = app.deps.tasks.create("automation", "test")
    await AutomationCapability(app.deps).handle(Request(
        text="add a pack of AA batteries to my amazon basket", args={"objective": _batteries()},
        ctx=app.deps.tool_context(task=task), task=task))
    brief = prompts[0]
    assert "A recipe (“Add a product to the Amazon basket”) started on this." in brief
    assert '[jv8] select "Quantity"' in brief, "the operator starts from the page the skill reached"
    assert "Tips for this site or app:" in brief and "Sponsored" in brief


async def test_the_operator_can_run_a_skill_as_one_tool(app, shop, fake_provider):
    replies = [json.dumps({"tool": "skill_amazon_add_to_basket", "arguments": {"query": "AA batteries"}}),
               json.dumps({"tool": "finish", "arguments": {"summary": "Added.",
                                                            "evidence": ["Added to Basket"]}})]
    prompts: list[str] = []

    def reply(messages, kwargs):
        text = "\n".join(m.content for m in messages)
        if "You operate this Mac" in text:
            prompts.append(text)
            return replies.pop(0)
        return None

    fake_provider.router = reply
    library = SkillLibrary()
    result = await Operator(app.deps).run(
        "add AA batteries to my Amazon basket", app.deps.tool_context(), tools=["browse_to"],
        objective=Objective(goal="add AA batteries to my Amazon basket"), background=True,
        skills=[library.get("amazon-add-to-basket")], budget=Budget(steps=10, model_calls=5))
    assert result.status == "finished" and shop.basket == ["B0AABAT24A"]
    assert "skill_amazon_add_to_basket" in prompts[0] and "Recipes:" in prompts[0]
    assert "The recipe finished." in prompts[1]
    assert result.used_skills == ["amazon-add-to-basket"]


# ---------------------------------------------------------------------------
# learning
# ---------------------------------------------------------------------------
SEARCH_VIEW = ('[jv1] field "Search Amazon.co.uk"\n'
               '[jv3] link "Duracell Plus AA Batteries, Pack of 24" → www.amazon.co.uk/dp/B0AABAT24A')
PRODUCT_VIEW = '[jv9] button "Add to Basket"\n[jv10] button "Buy Now"'


def _trail():
    return [
        {"tool": "browse_to", "arguments": {"url": "https://www.amazon.co.uk/s?k=AA+batteries"},
         "ok": True, "view": ""},
        {"tool": "read_page_manifest", "arguments": {}, "ok": True, "view": ""},
        {"tool": "click_page_element", "arguments": {"handle": "jv3"}, "ok": True, "view": SEARCH_VIEW},
        {"tool": "click_page_element", "arguments": {"handle": "jv77"}, "ok": False, "view": PRODUCT_VIEW},
        {"tool": "click_page_element", "arguments": {"handle": "jv9"}, "ok": True, "view": PRODUCT_VIEW},
    ]


def test_a_proven_run_becomes_a_recipe_in_terms_of_what_was_on_screen(app):
    skill = learn(_batteries(), _trail(), ["Added to Basket", "Clicked “Add to Basket”. Now on: x"],
                  app.deps.registry)
    assert skill is not None and skill.source == "learned"
    assert skill.steps == [
        {"go": "https://www.amazon.co.uk/s?k={query|url}"},
        {"click": {"role": "link", "best_match": "{query}", "href": "/dp/"}},
        {"click": {"text": "Add to Basket", "role": "button"}},
    ]
    assert skill.sites == ["amazon"] and skill.done_when == ["Added to Basket"]
    assert [p.name for p in skill.params] == ["query"]


def test_a_run_that_did_something_a_recipe_should_not_replay_is_not_learned(app):
    trail = _trail() + [{"tool": "send_email", "arguments": {"to": ["x@y.z"]}, "ok": True, "view": ""}]
    assert learn(_batteries(), trail, ["Added to Basket"], app.deps.registry) is None


def test_proof_is_kept_only_where_it_would_show_again():
    phrases = proof_phrases(["Added to Basket", "Subtotal (1 item): £14.99",
                             "Duracell AA Batteries added to your basket"], "AA batteries")
    assert phrases == ["Added to Basket"]


async def test_a_learned_skill_is_saved_reused_with_a_new_subject_and_forgotten(app, shop, tmp_path):
    library = SkillLibrary(tmp_path / "learned")
    skill = learn(_batteries(), _trail(), ["Added to Basket"], app.deps.registry)
    library.save(skill)
    reloaded = SkillLibrary(tmp_path / "learned")
    assert any(s.id == skill.id for s in reloaded.skills())
    found = reloaded.direct(Objective(goal="add tea to my amazon basket", targets=["AA batteries"]))
    assert found is not None
    outcome = await SkillRunner(app.deps, app.deps.tool_context()).run(*found)
    assert outcome.ok and shop.basket == ["B0AABAT24A"]
    assert reloaded.forget(skill.id)
    assert not list((tmp_path / "learned").glob("*.json"))
    assert reloaded.forget(skill.id) is False


def test_a_learned_skill_that_keeps_failing_is_set_aside(tmp_path):
    library = SkillLibrary(tmp_path / "learned")
    skill = load({"id": "learned-shop-add", "title": "Learned", "sites": ["shop"], "words": ["add"],
                  "steps": [{"go": "https://shop.example"}, {"click": {"text": "Add"}}]},
                 source="learned")
    library.save(skill)
    library.record(skill, False)
    assert library.get(skill.id) is not None
    library.record(skill, False)
    assert library.get(skill.id) is None, "two failures in a row: set aside"
    assert library.listing()[0]["set_aside"] is True


async def test_an_errand_that_finished_and_proved_it_teaches_a_skill(app, fake_provider, monkeypatch):
    app.config_store.update({"security": {"confirmation_timeout_s": 0.2}})
    shop = Shop()
    shop.install(app, monkeypatch)
    replies = [
        {"tool": "browse_to", "arguments": {"url": "https://www.amazon.co.uk/s?k=AA+batteries"}},
        {"tool": "click_page_element", "arguments": {"handle": "jv3", "label": "Duracell"}},
        {"tool": "click_page_element", "arguments": {"handle": "jv9", "label": "Add to Basket"}},
        {"tool": "finish", "arguments": {"summary": "Added.", "evidence": ["Added to Basket"]}},
    ]

    def reply(messages, kwargs):
        if "You operate this Mac" in "\n".join(m.content for m in messages):
            return json.dumps(replies.pop(0))
        return None

    fake_provider.router = reply
    objective = Objective(goal="add a pack of AA batteries to my shop basket", site="amazon.co.uk",
                          targets=["AA batteries"])
    monkeypatch.setattr(app.deps.skills, "direct", lambda *a, **k: None)   # make the operator do it
    task = app.deps.tasks.create("automation", "test")
    response = await AutomationCapability(app.deps).handle(Request(
        text="add a pack of AA batteries to my shop basket", args={"objective": objective},
        ctx=app.deps.tool_context(task=task), task=task))
    assert response.data["status"] == "finished"
    learned = response.data.get("learned")
    assert learned and learned.startswith("learned-amazon-")
    assert (Path(app.config.workspace_path) / "skills" / "learned").exists()


def test_the_skills_api_lists_and_forgets(app, tmp_path):
    from fastapi.testclient import TestClient
    from jarvis.server import create_app

    skill = load({"id": "learned-shop-add", "title": "Learned: add {query}", "sites": ["shop"],
                  "words": ["add"], "steps": [{"go": "https://shop.example"}, {"click": {"text": "Add"}}]},
                 source="learned")
    app.deps.skills.save(skill)
    api = create_app(app)
    with TestClient(api, headers={"X-Jarvis-Token": api.state.session_token}) as client:
        listed = client.get("/api/skills").json()
        assert any(s["id"] == "learned-shop-add" and s["source"] == "learned" for s in listed["skills"])
        assert client.delete("/api/skills/learned-shop-add").json() == {"forgotten": True}
        builtin = client.delete("/api/skills/amazon-search").json()
        assert builtin == {"forgotten": False}, "built-in skills can be disabled, not deleted"


def test_your_own_recipes_load_from_the_workspace_but_may_not_call_tools(tmp_path):
    (tmp_path / "mine.yaml").write_text(
        "skills:\n"
        "  - id: argos-search\n    title: Search Argos\n    sites: [argos]\n    words: [search, find]\n"
        "    params: {query: {from: target}}\n"
        "    steps: [{go: 'https://www.argos.co.uk/search/{query|url}/'}]\n"
        "  - id: sneaky\n    title: Sneaky\n    sites: [argos]\n    steps: [{tool: run_shell_command}]\n",
        encoding="utf-8")
    library = SkillLibrary(tmp_path / "learned", user_dir=tmp_path)
    found = library.direct(Objective(goal="find a kettle on argos", targets=["kettle"]))
    assert found and found[0].id == "argos-search" and found[0].source == "user"
    assert library.get("sneaky") is None, "a recipe you wrote can't call tools directly"

