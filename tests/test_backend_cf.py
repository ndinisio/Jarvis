"""MacAXBackend against a C API in which handing nothing to CoreFoundation is fatal.

On a Mac, PyObjC turns ``None`` into a NULL ``CFTypeRef`` and ``CFGetTypeID(NULL)`` is a segmentation
fault: not an exception, so no ``try``/``except`` in the backend or the harness can catch it, and the
process exits 139. The v8.54 diagnostics did exactly that for every attribute an element does not have.

This file cannot make Python segfault, so it stands in for the fault: ``StrictCF`` raises
``NullDereference`` (a ``BaseException``, which ``except Exception`` does not swallow, like a signal)
whenever a call is made with nothing, and the real ``MacAXBackend`` class is driven against it. What it
proves is the contract - nothing is ever passed to CoreFoundation or the Accessibility C API as nil -
not the fault itself; the tests in ``test_check_native.py`` apply the same stand-in to the harness.
"""

from __future__ import annotations

import pytest
from jarvis.surfaces.native.backend import MacAXBackend

AX, AXVALUE, ARRAY, STRING, NUMBER, OTHER = 1, 2, 3, 4, 5, 6


class NullDereference(BaseException):
    """What a segmentation fault is, as far as Python code can tell: nothing catches it."""


class AXRef:
    """An AXUIElementRef, with the attributes the fake application gives it."""

    def __init__(self, **attrs):
        self.attrs = attrs


class Array:
    """What PyObjC returns for an NSArray: a sequence, but not a ``list``."""

    def __init__(self, items=()):
        self.items = list(items)

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)

    def __bool__(self):
        return bool(self.items)

    def __getitem__(self, index):
        return self.items[index]


class StrictCF:
    """CoreFoundation, where a NULL argument kills the process."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def _check(self, name, *values):
        for value in values:
            self.calls.append((name, type(value).__name__))
            if value is None:
                raise NullDereference(f"{name}(NULL)")

    def CFGetTypeID(self, value):
        self._check("CFGetTypeID", value)
        if isinstance(value, AXRef):
            return AX
        if isinstance(value, Array):
            return ARRAY
        if isinstance(value, str):
            return STRING
        if isinstance(value, (bool, int, float)):
            return NUMBER
        return OTHER

    def CFArrayGetTypeID(self):
        return ARRAY

    def CFHash(self, value):
        self._check("CFHash", value)
        return id(value) & 0xFFFF

    def CFEqual(self, a, b):
        self._check("CFEqual", a, b)
        return a is b


class StrictAS:
    """ApplicationServices, where the same is true of every function that takes an element."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def _element(self, name, element):
        self.calls.append((name, type(element).__name__))
        if element is None:
            raise NullDereference(f"{name}(NULL)")
        return element

    def AXUIElementGetTypeID(self):
        return AX

    def AXValueGetTypeID(self):
        return AXVALUE

    def AXUIElementCopyAttributeNames(self, element, _out):
        return 0, list(self._element("AXUIElementCopyAttributeNames", element).attrs)

    def AXUIElementCopyMultipleAttributeValues(self, element, names, _options, _out):
        attrs = self._element("AXUIElementCopyMultipleAttributeValues", element).attrs
        return 0, [attrs.get(name) for name in names]

    def AXUIElementCopyAttributeValue(self, element, name, _out):
        attrs = self._element("AXUIElementCopyAttributeValue", element).attrs
        return (0, attrs[name]) if name in attrs else (-25205, None)

    def AXUIElementCopyActionNames(self, element, _out):
        return 0, ["AXPress"] if self._element("AXUIElementCopyActionNames", element) else []

    def AXUIElementPerformAction(self, element, action):
        self._element("AXUIElementPerformAction", element)
        return 0

    def AXUIElementSetAttributeValue(self, element, name, value):
        self._element("AXUIElementSetAttributeValue", element)
        return 0


def strict_backend() -> tuple[MacAXBackend, StrictAS, StrictCF]:
    """The real backend class on the strict C API (it needs no PyObjC to be built this way)."""
    backend = MacAXBackend.__new__(MacAXBackend)
    backend.AS, backend.CF = StrictAS(), StrictCF()
    backend._value_type_id, backend._multi = AXVALUE, True
    return backend, backend.AS, backend.CF


#: Every kind of value an attribute can come back as, and a few it cannot.
VALUES = {
    "nothing": None, "empty text": "", "text": "Desktop — iCloud", "zero": 0, "number": 12, "float": 0.5,
    "true": True, "false": False, "point": (300.0, 120.0), "bytes": b"x", "dict": {},
    "element": AXRef(AXRole="AXButton"), "array of elements": Array([AXRef(), AXRef()]),
    "empty array": Array(), "array of words": Array(["a", "b"]), "plain list": [], "object": object(),
}


def test_the_stand_in_really_is_fatal_for_nothing_and_for_nothing_else():
    cf = StrictCF()
    with pytest.raises(NullDereference):
        cf.CFGetTypeID(None)
    assert not isinstance(NullDereference(), Exception), "so `except Exception` cannot hide it"
    for name, value in VALUES.items():
        if value is not None:
            cf.CFGetTypeID(value)


@pytest.mark.parametrize("name", list(VALUES))
def test_no_kind_of_value_makes_is_element_or_linked_elements_touch_the_c_api_with_nothing(name):
    backend, _, cf = strict_backend()
    value = VALUES[name]
    assert backend.is_element(value) is (name == "element")
    backend.linked_elements(value)
    assert all(kind != "NoneType" for _, kind in cf.calls)


def test_plain_python_values_are_not_even_shown_to_coreFoundation():
    backend, _, cf = strict_backend()
    for name in ("nothing", "empty text", "text", "zero", "number", "float", "true", "false", "point", "bytes", "dict"):
        assert backend.is_element(VALUES[name]) is False
        assert backend.linked_elements(VALUES[name]) is None
    assert cf.calls == [], "a value Python can see is not an element needs no answer from CoreFoundation"


def test_an_element_links_to_itself_and_an_array_of_elements_to_its_items():
    backend, _, _ = strict_backend()
    one, two = AXRef(), AXRef()
    assert backend.linked_elements(one) == [one]
    assert backend.linked_elements(Array([one, two])) == [one, two], "an NSArray proxy is not a list"


def test_arrays_that_are_not_made_of_elements_link_to_nothing():
    backend, _, _ = strict_backend()
    assert backend.linked_elements(Array()) is None
    assert backend.linked_elements(Array(["a", "b"])) is None
    assert backend.linked_elements(Array([AXRef(), "b"])) is None
    assert backend.linked_elements(object()) is None


def test_reading_attributes_with_missing_ones_never_reaches_the_c_api_with_nothing():
    backend, _, cf = strict_backend()
    item = AXRef(AXRole="AXMenuItem", AXTitle="Desktop — iCloud", AXHelp=None, AXIdentifier=None,
                 AXEnabled=True, AXPosition=(1.0, 2.0), AXParent=AXRef(), AXChildren=Array())
    names = backend.attribute_names(item)
    values = backend.attributes(item, tuple(names) + ("AXNotThere",))
    assert values["AXHelp"] is None and values["AXTitle"] == "Desktop — iCloud"
    assert all(kind != "NoneType" for _, kind in cf.calls)
    assert backend.attribute(item, "AXNotThere") is None


def test_every_method_that_takes_an_element_answers_for_nothing_without_calling_the_c_api():
    backend, accessibility, cf = strict_backend()
    assert backend.attribute(None, "AXRole") is None
    assert backend.attributes(None, ("AXRole",)) == {}
    assert backend.attribute_names(None) == []
    assert backend.actions(None) == []
    assert backend.perform(None, "AXPress") is False
    assert backend.set_attribute(None, "AXFocused", True) is False
    assert backend.key(None) == 0
    assert backend.same(None, None) is True
    assert backend.same(None, AXRef()) is False and backend.same(AXRef(), None) is False
    assert accessibility.calls == [] and cf.calls == []


def test_the_same_methods_still_work_for_a_real_element():
    backend, _, _ = strict_backend()
    element = AXRef(AXRole="AXButton")
    assert backend.actions(element) == ["AXPress"]
    assert backend.perform(element, "AXPress") is True
    assert backend.set_attribute(element, "AXFocused", True) is True
    assert backend.same(element, element) is True and backend.same(element, AXRef()) is False
    assert backend.key(element) == backend.key(element)


def test_a_value_the_c_api_could_not_name_is_not_an_element_rather_than_an_error():
    backend, _, _ = strict_backend()

    class Broken(StrictCF):
        def CFGetTypeID(self, value):
            raise RuntimeError("not a CF type")

    backend.CF = Broken()
    assert backend.is_element(object()) is False
    assert backend.linked_elements(object()) is None
