"""Classifying which tool calls must always be confirmed individually."""

from __future__ import annotations

from jarvis.security import consequence
from jarvis.tools.base import ToolSpec


def _spec(**overrides) -> ToolSpec:
    return ToolSpec(name="t", description="d", **overrides)


def test_a_tool_marked_always_confirm_individually_is_always_consequential():
    assert consequence.classify("anything", {}, _spec(always_confirm_individually=True)) is True


def test_fixed_consequential_tools_are_always_flagged_regardless_of_arguments():
    for name in ("run_installer", "send_email", "delete_file"):
        assert consequence.classify(name, {"path": "x"}, _spec()) is True


def test_ordinary_tools_are_not_consequential_by_default():
    assert consequence.classify("open_application", {"name": "Safari"}, _spec()) is False


def test_a_checkout_labelled_click_is_consequential():
    for label in ("Buy Now", "Place Order", "Proceed to checkout", "Confirm Purchase",
                  "Pay Now", "Complete Purchase"):
        assert consequence.classify("click_page_element", {"handle": "7", "label": label},
                                    _spec()) is True


def test_bare_purchase_verbs_are_consequential_not_only_the_now_suffixed_forms():
    """Real buttons are as often labelled just "Buy" or "Buy It Now" as
    "Buy Now" — a pattern requiring "buy" immediately adjacent to "now"
    would silently miss "Buy It Now" (the actual eBay button text) and
    bare "Buy"/"Pay"/"Purchase" labels entirely."""
    for label in ("Buy", "Buy It Now", "Pay", "Purchase", "Order Now"):
        assert consequence.classify("click_page_element", {"handle": "7", "label": label},
                                    _spec()) is True, label


def test_add_to_basket_is_deliberately_not_consequential():
    """Placing an item in a basket for the user to review is explicitly
    allowed to be a routine, task-approved step — only checkout/payment
    vocabulary escalates."""
    for label in ("Add to Basket", "Add to Cart", "Add to Wish List"):
        assert consequence.classify("click_page_element", {"handle": "3", "label": label},
                                    _spec()) is False


def test_only_the_labels_own_wording_is_inspected_not_typed_content():
    """The text a user is having JARVIS type into a field is arbitrary
    content, not a control's identity — checking it would false-positive on
    something as ordinary as searching for a product called a "checkout
    scanner"."""
    assert consequence.classify(
        "fill_page_field", {"handle": "1", "label": "Search", "text": "checkout scanner"}, _spec()
    ) is False


def test_a_delete_or_send_labelled_click_is_consequential():
    assert consequence.classify("click_element", {"label": "Delete permanently"}, _spec()) is True
    assert consequence.classify("click_element", {"label": "Send"}, _spec()) is True


# -- v3.0: the real element decides, not the model's description of it --------

def test_the_real_element_overrides_a_misleading_label():
    """A model that calls the Buy Now button "Add to Basket" is judged on
    what the button really is."""
    target = {"role": "button", "text": "Buy Now", "id": "buy-now-button", "name": "submit.buy-now"}
    assert consequence.classify("click_page_element", {"handle": "jv9", "label": "Add to Basket"},
                                _spec(), target) is True


def test_identifiers_are_read_as_words():
    target = {"role": "button", "text": "Continue", "name": "proceedToRetailCheckout"}
    assert consequence.classify("click_page_element", {"handle": "1"}, _spec(), target) is True


def test_add_to_basket_stays_routine():
    target = {"role": "button", "text": "Add to Basket", "id": "add-to-cart-button",
              "name": "submit.add-to-cart", "action": "https://www.amazon.co.uk/cart/add"}
    assert consequence.classify("click_page_element", {"handle": "1"}, _spec(), target) is False


def test_a_form_that_submits_to_checkout_is_consequential_even_with_a_bland_button():
    target = {"role": "button", "text": "Continue", "action": "https://shop.example.com/checkout/pay"}
    assert consequence.classify("submit_page_form", {"handle": "1"}, _spec(), target) is True


def test_navigating_straight_to_a_checkout_page_is_consequential():
    assert consequence.classify("browse_to", {"url": "https://www.amazon.co.uk/gp/buy/spc"}, _spec())
    assert not consequence.classify("browse_to", {"url": "https://www.amazon.co.uk/s?k=pay+as+you+go"},
                                    _spec())


def test_ticking_a_checkbox_is_a_choice_not_an_action():
    """"Send me the newsletter" and a to-do item called "Send the invoice"
    are checkboxes: ticking them sends nothing."""
    for text in ("Send me the newsletter", "Send the invoice", "Delete my data after 30 days"):
        target = {"role": "checkbox", "text": text}
        assert consequence.classify("click_page_element", {"handle": "1", "label": text},
                                    _spec(), target) is False, text


def test_typing_into_a_terminal_is_always_consequential():
    for app in ("Terminal", "iTerm2", "Script Editor", "Keychain Access"):
        assert consequence.classify("type_text", {"text": "ls"}, _spec(), {"application": app}), app
    assert not consequence.classify("type_text", {"text": "hello"}, _spec(), {"application": "Notes"})
