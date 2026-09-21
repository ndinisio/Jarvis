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
