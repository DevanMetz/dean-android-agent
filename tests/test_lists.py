import tools


def box():
    tb = tools.Toolbox.__new__(tools.Toolbox)  # lists don't need the network pieces
    import threading
    tb.lock = threading.Lock()
    return tb


def test_add_dedupes_and_normalises_list_names():
    tb = box()
    assert tb.list_add(["milk", "Eggs"], "shopping list")["list"] == "grocery"
    r = tb.list_add(["MILK", "bread"], "groceries")
    assert r["added"] == ["bread"] and r["now_has"] == 3
    assert tb.list_show("grocery")["items"] == ["milk", "Eggs", "bread"]


def test_check_off_matches_loosely_and_clear():
    tb = box()
    tb.list_add(["2% milk", "eggs", "paper towels"])
    assert tb.list_remove(["milk"])["removed"] == ["2% milk"]
    tb.list_add(["call the plumber"], "todo")
    assert tb.list_show() == {"grocery": ["eggs", "paper towels"], "to-do": ["call the plumber"]}
    assert tb.list_clear("grocery")["cleared"] == 2
    assert tb.list_show() == {"to-do": ["call the plumber"]}
