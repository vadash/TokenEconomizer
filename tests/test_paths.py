"""`paths.truthy`: the one spelling of every TE_* on/off gate."""
from tokeneconomizer.plugin.paths import truthy


def test_truthy_reads_one_true_yes_on():
    for v in ("1", "true", "True", "TRUE", " True ", "yes", "YES", " yes ", "on", "On",
              "ON", " on "):
        assert truthy(v) is True


def test_truthy_rejects_the_rest():
    for v in ("0", "false", "False", " FALSE ", "no", "off", "", "   ", None, "2",
              "enable", "truish"):
        assert truthy(v) is False
