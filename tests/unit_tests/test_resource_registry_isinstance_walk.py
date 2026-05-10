"""Regression tests for ``ResourceRegistry.get_by_type`` MRO walk.

Background: ``get_by_type`` previously used exact-class lookup
(``self._by_type.get(cls, [])``). Registering a ``BigQueryDataSource``
and then asking for ``DataSource`` would return ``[]`` — silently
breaking ``WorkflowAgent._bind_tools``'s call to
``self.get_resources_by_type(DataSource)`` (which expects subclass
matches).

Fix: keep the exact-class fast path but add an isinstance fallback so
``get_by_type(BaseClass)`` returns every registered instance whose
type IS-A ``BaseClass``.
"""
from __future__ import annotations

from ryoma_ai.agent.resource_registry import ResourceRegistry


class _Base:
    """Distinct base class so we don't pollute production type names."""


class _Derived(_Base):
    """Direct subclass."""


class _DeepDerived(_Derived):
    """Two levels deep."""


class _Unrelated:
    """Same registry; no inheritance with _Base."""


def test_get_by_type_returns_exact_match_unchanged():
    """The fast path: registering ``_Derived`` then asking for ``_Derived``
    must still return that instance (we keep this behavior)."""
    reg = ResourceRegistry()
    obj = _Derived()
    reg.register(obj)
    assert reg.get_by_type(_Derived) == [obj]


def test_get_by_type_walks_mro_for_subclass_lookup():
    """The new behavior: registering ``_Derived`` then asking for ``_Base``
    must return the derived instance via isinstance walk. Pre-fix:
    returned [].
    """
    reg = ResourceRegistry()
    obj = _Derived()
    reg.register(obj)
    assert reg.get_by_type(_Base) == [obj], (
        "get_by_type(BaseClass) must return registered subclass instances. "
        "Pre-fix this returned [] because exact-class indexing missed "
        "subclasses. This silently broke WorkflowAgent._bind_tools' "
        "get_resources_by_type(DataSource) call."
    )


def test_get_by_type_walks_multi_level_mro():
    """Two levels deep also works: register ``_DeepDerived``, ask for
    ``_Base``."""
    reg = ResourceRegistry()
    obj = _DeepDerived()
    reg.register(obj)
    assert reg.get_by_type(_Base) == [obj]


def test_get_by_type_excludes_unrelated_classes():
    """An unrelated class registered alongside must not be returned by
    a base-class lookup. Guards against an over-broad isinstance walk."""
    reg = ResourceRegistry()
    derived_obj = _Derived()
    unrelated_obj = _Unrelated()
    reg.register(derived_obj)
    reg.register(unrelated_obj)
    base_results = reg.get_by_type(_Base)
    assert derived_obj in base_results
    assert unrelated_obj not in base_results


def test_get_by_type_returns_all_subclass_instances():
    """Multiple subclasses registered: base-class lookup returns them all."""
    reg = ResourceRegistry()
    a = _Derived()
    b = _DeepDerived()
    reg.register(a)
    reg.register(b)
    base_results = reg.get_by_type(_Base)
    assert a in base_results
    assert b in base_results
    assert len(base_results) == 2


def test_get_by_type_returns_empty_for_unregistered_type():
    """A class with no registered instances returns []."""
    reg = ResourceRegistry()
    reg.register(_Derived())

    class _NeverRegistered:
        pass

    assert reg.get_by_type(_NeverRegistered) == []
