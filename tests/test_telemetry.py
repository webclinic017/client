"""
telemetry lib tests.
"""

import wandb

telem_lib = wandb.sdk.lib.telemetry


def test_telemetry_parse():
    pf = telem_lib._parse_label_lines
    print("hi")

    assert pf(["nothin", "dontcare", "@wandbcode{hello}"]) == dict(id="hello")
    assert pf(["", "  @wandbcode{hi-there, junk=2}"]) == dict(id="hi_there", junk="2")
    assert pf(["@wandbcode{hello, junk=2}"]) == dict(id="hello", junk="2")
    assert pf(["@wandbcode{}", "junk", "@wandbcode{ignore}"]) == dict()
    assert pf(['@wandbcode{h, j="iquote", p=hhh}']) == dict(id="h", j="iquote", p="hhh")
    assert pf(['@wandbcode{h, j="i,e", p=hhh}']) == dict(id="h", p="hhh")
    assert pf(["@wandbcode{j=i-p,"]) == dict(j="i_p")
    o
