from types import SimpleNamespace as D

from app.core import scene_graph as SG


def det(label, score, box):
    return D(label=label, score=score, box=box)


def kinds(scene):
    return {(r.a, r.kind, r.b) for r in scene.relations}


def test_containment_and_on_and_sides():
    s = SG.build([det("dining table", 0.9, (0, 100, 400, 300)), det("laptop", 0.8, (100, 40, 250, 100)),
                  det("cup", 0.7, (150, 150, 180, 190)), det("tv", 0.6, (500, 0, 700, 120))], (800, 600), now=1.0)
    k = kinds(s)
    assert ("cup#1", "inside", "dining_table#1") in k and ("dining_table#1", "contains", "cup#1") in k
    assert ("laptop#1", "on", "dining_table#1") in k
    assert any(r.kind == "left_of" and r.a == "dining_table#1" and r.b == "tv#1" for r in s.relations)
    assert all(n.source == "detector" and n.at == 1.0 for n in s.nodes)
    assert "2-D boxes only" in s.text()


def test_duplicate_labels_get_numbered_ids_and_overlap():
    s = SG.build([det("person", 0.9, (0, 0, 100, 200)), det("person", 0.8, (80, 0, 180, 200))])
    assert [n.id for n in s.nodes] == ["person#1", "person#2"]
    assert ("person#1", "overlaps", "person#2") in kinds(s)


def test_empty_and_cap():
    assert "nothing detected" in SG.build([]).text()
    many = [det("cup", 0.5 + i / 1000, (i * 10, 0, i * 10 + 5, 5)) for i in range(60)]
    s = SG.build(many)
    assert len(s.nodes) == SG.MAX_NODES and any("most confident" in n for n in s.notes)


def test_diff_appeared_gone_moved():
    a = SG.build([det("cup", 0.9, (0, 0, 50, 50)), det("book", 0.8, (100, 0, 150, 50))])
    b = SG.build([det("cup", 0.9, (200, 0, 250, 50)), det("phone", 0.7, (0, 100, 30, 150))])
    c = SG.diff(a, b)
    assert [n.label for n in c.appeared] == ["phone"] and [n.label for n in c.gone] == ["book"] and [o.label for o, _ in c.moved] == ["cup"]
    assert "look-alike" in c.text()
    assert not SG.diff(a, a).any and "No visible change" in SG.diff(a, a).text()
