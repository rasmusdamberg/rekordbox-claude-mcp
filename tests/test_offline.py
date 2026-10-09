"""Tests that do not need a Rekordbox database."""

import importlib
import os

import pytest


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    monkeypatch.setenv("RB_DB_DIR", str(tmp_path))
    import rekordbox_claude_mcp as m

    return importlib.reload(m)


def test_smart_xml_matches_rekordbox_format(mod):
    # Exact XML Rekordbox 7 wrote for playlist 4078795266 (MyTag + BPM 120-128).
    conds = [
        {"field": "mytag", "op": "contains", "left": "1717227854", "right": "", "unit": ""},
        {"field": "bpm", "op": "in_range", "left": "12000", "right": "12800", "unit": "0"},
    ]
    assert mod.smart_xml("4078795266", conds, "all") == (
        '<NODE Id="-216172030" LogicalOperator="1" AutomaticUpdate="0">'
        '<CONDITION PropertyName="myTag" Operator="8" ValueUnit="" ValueLeft="1717227854" ValueRight=""/>'
        '<CONDITION PropertyName="bpm" Operator="5" ValueUnit="0" ValueLeft="12000" ValueRight="12800"/>'
        "</NODE>"
    )


def test_smart_xml_any_and_small_id(mod):
    xml = mod.smart_xml("76868619", [], "any")
    assert 'Id="76868619"' in xml and 'LogicalOperator="2"' in xml


def test_hex_ids(mod):
    assert mod._hex("4078795266") == "F31D7A02"
    assert mod._hex("root") == "0"


XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\r\n\r\n'
    '<MASTER_PLAYLIST Version="3.0.0" AutomaticSync="0">\r\n'
    '  <PRODUCT Name="rekordbox" Version="7.0.2" Company="Pioneer DJ"/>\r\n'
    "  <PLAYLISTS>\r\n"
    '    <NODE Id="A1" ParentId="0" Attribute="1" Timestamp="111" Lib_Type="0" CheckType="0"/>\r\n'
    '    <NODE Id="B2" ParentId="0" Attribute="0" Timestamp="222" Lib_Type="0" CheckType="0"/>\r\n'
    "  </PLAYLISTS>\r\n"
    "</MASTER_PLAYLIST>\r\n"
)


def test_master_xml_add_move_touch_keeps_crlf(mod, tmp_path):
    p = tmp_path / "masterPlaylists6.xml"
    p.write_bytes(XML.encode())
    mod.update_master_xml([("add", str(0xC3), str(0xA1), 0), ("move", str(0xB2), str(0xA1)), ("touch", "999")])
    out = p.read_bytes().decode()
    assert "\n" not in out.replace("\r\n", "")  # still CRLF only
    assert '<NODE Id="C3" ParentId="A1" Attribute="0"' in out
    assert '<NODE Id="B2" ParentId="A1"' in out
    assert 'Id="A1" ParentId="0" Attribute="1" Timestamp="111"' in out  # untouched node unchanged
    assert out.index('Id="C3"') < out.index("</PLAYLISTS>")


def test_commit_refuses_when_writes_disabled(mod, monkeypatch):
    monkeypatch.setattr(mod, "WRITES_ENABLED", False)
    assert mod.rb_commit(["x"])["ok"] is False


@pytest.mark.skipif(not os.environ.get("RB_TEST_DB_DIR"), reason="set RB_TEST_DB_DIR to a COPY of a rekordbox folder")
def test_roundtrip_on_copy(tmp_path, monkeypatch):
    import shutil

    work = tmp_path / "rb"
    shutil.copytree(os.environ["RB_TEST_DB_DIR"], work)
    monkeypatch.setenv("RB_DB_DIR", str(work))
    monkeypatch.setenv("RB_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("RB_ENABLE_WRITES", "1")
    monkeypatch.setenv("RB_IGNORE_RUNNING", "1")
    import rekordbox_claude_mcp as m

    m = importlib.reload(m)
    col = next(iter(m.rb_list_mytags()))
    a1 = m.rb_preview_create_folder("ZZ TEST")
    a2 = m.rb_preview_create_mytag(col, "ZZ Tag")
    assert m.rb_commit([a1["action_id"], a2["action_id"]])["ok"]
    tracks = m.rb_search_tracks(limit=3)["tracks"]
    a3 = m.rb_preview_create_playlist("ZZ List", parent="ZZ TEST", track_ids=[t["id"] for t in tracks])
    a4 = m.rb_preview_tag_tracks(f"{col}/ZZ Tag", track_ids=[t["id"] for t in tracks])
    a5 = m.rb_preview_create_smart_playlist(
        "ZZ Smart", [m.SmartCondition(field="mytag", op="contains", value=f"{col}/ZZ Tag")], parent="ZZ TEST"
    )
    assert a5["matches_now"] == 0
    assert m.rb_commit([a3["action_id"], a4["action_id"], a5["action_id"]])["ok"]
    assert m.rb_get_playlist_tracks("ZZ TEST/ZZ List")["count"] == len(tracks)
    assert m.rb_search_tracks(mytag=f"{col}/ZZ Tag")["total"] == len(tracks)
