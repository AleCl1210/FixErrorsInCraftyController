from app.versions import compare, parse_spec, satisfies, spec_to_text


def test_compare():
    assert compare("1.20.1", "1.20.1") == 0
    assert compare("1.20.1", "1.21") < 0
    assert compare("1.9", "1.10") < 0


def test_wildcard_spec():
    assert satisfies("1.20.1", "1.20.x") is True
    assert satisfies("1.20", "1.20.x") is True
    assert satisfies("1.21", "1.20.x") is False
    assert satisfies("1.19.4", "1.19.x") is True


def test_maven_ranges():
    assert satisfies("1.20.1", "[1.20.1,1.21)") is True
    assert satisfies("1.21", "[1.20.1,1.21)") is False
    assert satisfies("1.19.4", "[1.19.4]") is True
    assert satisfies("1.20.1", "[1.19.4]") is False
    assert satisfies("1.20.4", "[1.20,)") is True
    assert satisfies("1.19", "[1.20,)") is False


def test_simple_constraints():
    assert satisfies("1.20.1", ">=1.20") is True
    assert satisfies("1.19.2", ">=1.20") is False
    assert satisfies("1.20.1", "1.20.1") is True
    assert satisfies("1.20.2", "1.20.1") is False
    assert satisfies("1.20.5", "*") is True
    assert satisfies("1.20.5", "") is True
    assert satisfies("1.20.6", "~1.20.1") is True
    assert satisfies("1.21", "~1.20.1") is False
    assert satisfies("1.99", "^1.20.1") is True


def test_parse_spec_wildcard_details():
    constraints = parse_spec("1.20.x")
    assert (">=", "1.20") in constraints
    assert ("<", "1.21.0") in constraints or ("<", "1.21") in constraints


def test_spec_to_text():
    assert spec_to_text("") == "qualsiasi versione"
    assert spec_to_text("1.20.x") == "1.20.x"
