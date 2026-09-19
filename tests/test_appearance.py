from helios.backend.appearance import SCHEME_CHOICES, SCHEMES, normalize_scheme


def test_known_values_normalize_to_themselves():
    for value in SCHEMES:
        assert normalize_scheme(value) == value


def test_choice_keys_are_valid_schemes():
    for _label, value in SCHEME_CHOICES:
        assert value in SCHEMES
        assert normalize_scheme(value) == value


def test_bad_values_default_to_auto():
    for bad in ("bogus", None, 123, "", "DARK"):
        assert normalize_scheme(bad) == "auto"
