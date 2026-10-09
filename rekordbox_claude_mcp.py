"""Rekordbox MCP server.

Unofficial. Not affiliated with or endorsed by AlphaTheta / Pioneer DJ.

Read the Rekordbox 6/7 library (master.db) and, after explicit approval, write
playlists, folders, smart playlists and MyTags.

Safety model
- Every change is first a *preview* that returns an action id. Nothing is
  written until ``rb_commit`` is called with that id.
- Writes are disabled unless the environment variable RB_ENABLE_WRITES=1.
- Writes refuse to run while Rekordbox is open.
- A full backup of master.db (+ wal/shm and masterPlaylists6.xml) is taken
  before every commit. The newest RB_BACKUP_KEEP backups are kept.
- The server never deletes tracks from the collection and never deletes
  playlists.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import secrets
import shutil
import sys
import uuid as _uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

from mcp.server.fastmcp import FastMCP
from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import tables
from pyrekordbox.utils import get_rekordbox_pid
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, text

logging.getLogger("pyrekordbox").setLevel(logging.ERROR)


def _default_db_dir() -> str:
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", "~"), "Pioneer", "rekordbox")
    return "~/Library/Pioneer/rekordbox"


DB_DIR = Path(os.path.expanduser(os.environ.get("RB_DB_DIR") or _default_db_dir()))
DB_PATH = DB_DIR / "master.db"
BACKUP_DIR = Path(
    os.path.expanduser(os.environ.get("RB_BACKUP_DIR") or str(DB_DIR.parent / "rekordbox-claude-backups"))
)
BACKUP_KEEP = int(os.environ.get("RB_BACKUP_KEEP", "20"))
WRITES_ENABLED = os.environ.get("RB_ENABLE_WRITES", "") == "1"
# Only for testing against a copy of the database.
IGNORE_RUNNING = os.environ.get("RB_IGNORE_RUNNING", "") == "1"

mcp = FastMCP("rekordbox")

FILE_TYPES = {1: "mp3", 4: "m4a", 5: "flac", 11: "wav", 12: "aiff", 22: "tidal", 25: "spotify", 26: "apple-music"}


# --------------------------------------------------------------------------- helpers


@contextmanager
def open_db() -> Iterator[Rekordbox6Database]:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Rekordbox database not found: {DB_PATH} (set RB_DB_DIR)")
    db = Rekordbox6Database(str(DB_PATH), db_dir=str(DB_DIR))
    try:
        yield db
    finally:
        try:
            # close() discards uncommitted changes but keeps loaded attributes readable
            db.close()
        except Exception:
            pass
        try:
            # Release the file handles. A pooled connection kept open across calls
            # breaks the next open after Rekordbox rewrites master.db and removes
            # its -shm file ("disk I/O error").
            db.engine.dispose()
        except Exception:
            pass


def rekordbox_running() -> bool:
    if IGNORE_RUNNING:
        return False
    try:
        return bool(get_rekordbox_pid())
    except Exception:
        return False


def utcnow() -> _dt.datetime:
    # Rekordbox stores timestamps as UTC ("... +00:00").
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def new_uuid() -> str:
    return str(_uuid.uuid4())


def unused_id(db: Rekordbox6Database, table: Any) -> str:
    for _ in range(10000):
        n = int.from_bytes(secrets.token_bytes(4), "big")
        if n < 1000:
            continue
        if not db.session.query(table).filter(table.ID == str(n)).count():
            return str(n)
    raise RuntimeError("Could not generate an unused ID")


def tidal_id(c: tables.DjmdContent) -> Optional[str]:
    path = c.FolderPath or ""
    if path.startswith("tidal:tracks:"):
        return path.split(":")[-1]
    return None


def bpm(c: tables.DjmdContent) -> Optional[float]:
    return round(c.BPM / 100, 2) if c.BPM else None


def track_dict(c: tables.DjmdContent, tags: Optional[list[str]] = None) -> dict:
    d = {
        "id": c.ID,
        "title": c.Title,
        "artist": c.Artist.Name if c.Artist else None,
        "bpm": bpm(c),
        "key": c.Key.ScaleName if c.Key else None,
        "genre": c.Genre.Name if c.Genre else None,
        "year": c.ReleaseYear or None,
        "source": FILE_TYPES.get(c.FileType, str(c.FileType)),
    }
    t = tidal_id(c)
    if t:
        d["tidal_id"] = t
    if tags is not None:
        d["mytags"] = tags
    return d


def live(q: Any, table: Any) -> Any:
    return q.filter(or_(table.rb_local_deleted == 0, table.rb_local_deleted.is_(None)))


def playlists(db: Rekordbox6Database) -> list[tables.DjmdPlaylist]:
    return live(db.session.query(tables.DjmdPlaylist), tables.DjmdPlaylist).all()


def playlist_path(pl: tables.DjmdPlaylist, by_id: dict[str, tables.DjmdPlaylist]) -> str:
    parts = [pl.Name]
    cur = pl
    while cur.ParentID and cur.ParentID != "root" and cur.ParentID in by_id:
        cur = by_id[cur.ParentID]
        parts.append(cur.Name)
    return "/".join(reversed(parts))


def resolve_playlist(db: Rekordbox6Database, ref: str, folder: Optional[bool] = None) -> tables.DjmdPlaylist:
    """Find a playlist/folder by ID, full path ("A/B/C") or unique name."""
    pls = playlists(db)
    by_id = {p.ID: p for p in pls}
    ref = ref.strip()
    if ref in by_id:
        hit = [by_id[ref]]
    else:
        hit = [p for p in pls if playlist_path(p, by_id) == ref.strip("/")]
        if not hit:
            hit = [p for p in pls if p.Name == ref]
    if folder is True:
        hit = [p for p in hit if p.Attribute == 1]
    elif folder is False:
        hit = [p for p in hit if p.Attribute != 1]
    if not hit:
        raise ValueError(f"No {'folder' if folder else 'playlist'} found matching '{ref}'")
    if len(hit) > 1:
        paths = ", ".join(f"{playlist_path(p, by_id)} (id {p.ID})" for p in hit)
        raise ValueError(f"'{ref}' is ambiguous: {paths}. Use the full path or the id.")
    return hit[0]


def resolve_parent(db: Rekordbox6Database, parent: Optional[str]) -> str:
    if not parent or parent.strip() in ("", "/", "root"):
        return "root"
    return resolve_playlist(db, parent, folder=True).ID


def mytag_columns(db: Rekordbox6Database) -> list[tables.DjmdMyTag]:
    q = live(db.session.query(tables.DjmdMyTag), tables.DjmdMyTag)
    return q.filter(tables.DjmdMyTag.ParentID == "root").order_by(tables.DjmdMyTag.Seq).all()


def resolve_column(db: Rekordbox6Database, ref: str) -> tables.DjmdMyTag:
    for col in mytag_columns(db):
        if ref in (col.ID, col.Name) or col.Name.lower() == ref.lower():
            return col
    raise ValueError(f"No MyTag column found matching '{ref}'")


def resolve_tag(db: Rekordbox6Database, ref: str) -> tables.DjmdMyTag:
    """'Column/Tag', tag id or unique tag name."""
    q = live(db.session.query(tables.DjmdMyTag), tables.DjmdMyTag).filter(tables.DjmdMyTag.Attribute == 0)
    tags = q.all()
    cols = {c.ID: c.Name for c in mytag_columns(db)}
    if "/" in ref:
        cname, tname = ref.split("/", 1)
        hit = [t for t in tags if cols.get(t.ParentID, "").lower() == cname.strip().lower()
               and t.Name.lower() == tname.strip().lower()]
    else:
        hit = [t for t in tags if t.ID == ref] or [t for t in tags if t.Name.lower() == ref.strip().lower()]
    if not hit:
        raise ValueError(f"No MyTag found matching '{ref}'. Use 'Column/Tag'.")
    if len(hit) > 1:
        opts = ", ".join(f"{cols.get(t.ParentID)}/{t.Name}" for t in hit)
        raise ValueError(f"'{ref}' is ambiguous: {opts}")
    return hit[0]


def tag_label(db: Rekordbox6Database, t: tables.DjmdMyTag) -> str:
    cols = {c.ID: c.Name for c in mytag_columns(db)}
    return f"{cols.get(t.ParentID, '?')}/{t.Name}"


def resolve_tracks(db: Rekordbox6Database, track_ids: Optional[list[str]], tidal_ids: Optional[list[str]]) -> tuple[list[tables.DjmdContent], list[str]]:
    """Return (tracks, missing) for Rekordbox ids and/or Tidal ids."""
    found: dict[str, tables.DjmdContent] = {}
    missing: list[str] = []
    C = tables.DjmdContent
    for tid in track_ids or []:
        c = live(db.session.query(C), C).filter(C.ID == str(tid)).first()
        if c:
            found[c.ID] = c
        else:
            missing.append(f"rekordbox:{tid}")
    if tidal_ids:
        wanted = {str(t) for t in tidal_ids}
        paths = [f"tidal:tracks:{t}" for t in wanted]
        rows = live(db.session.query(C), C).filter(C.FolderPath.in_(paths)).all()
        got = set()
        for c in rows:
            found.setdefault(c.ID, c)
            got.add(tidal_id(c))
        missing += [f"tidal:{t}" for t in sorted(wanted - got)]
    return list(found.values()), missing


def tags_for(db: Rekordbox6Database, content_ids: list[str]) -> dict[str, list[str]]:
    if not content_ids:
        return {}
    S, T = tables.DjmdSongMyTag, tables.DjmdMyTag
    cols = {c.ID: c.Name for c in mytag_columns(db)}
    rows = (
        live(db.session.query(S.ContentID, T.Name, T.ParentID).join(T, T.ID == S.MyTagID), S)
        .filter(S.ContentID.in_(content_ids))
        .all()
    )
    out: dict[str, list[str]] = {}
    for cid, name, parent in rows:
        out.setdefault(cid, []).append(f"{cols.get(parent, '?')}/{name}")
    return out


# --------------------------------------------------------------------------- smart lists

SmartField = Literal["mytag", "bpm", "genre", "artist", "title", "year"]
SmartOp = Literal["contains", "not_contains", "equal", "not_equal", "greater", "less", "in_range", "starts_with", "ends_with"]

OP_CODES = {"equal": 1, "not_equal": 2, "greater": 3, "less": 4, "in_range": 5, "contains": 8,
            "not_contains": 9, "starts_with": 10, "ends_with": 11}
XML_PROP = {"mytag": "myTag", "bpm": "bpm", "genre": "genre", "artist": "artist", "title": "name", "year": "year"}
VALID_OPS = {
    "mytag": {"contains", "not_contains"},
    "bpm": {"equal", "not_equal", "greater", "less", "in_range"},
    "year": {"equal", "not_equal", "greater", "less", "in_range"},
    "genre": {"equal", "not_equal", "contains", "not_contains", "starts_with", "ends_with"},
    "artist": {"equal", "not_equal", "contains", "not_contains", "starts_with", "ends_with"},
    "title": {"equal", "not_equal", "contains", "not_contains", "starts_with", "ends_with"},
}
# Verified against a smart playlist created by Rekordbox 7 itself.
VERIFIED_FIELDS = {"mytag", "bpm"}


class SmartCondition(BaseModel):
    field: SmartField = Field(description="mytag, bpm, genre, artist, title or year")
    op: SmartOp = Field(description="mytag: contains/not_contains. bpm/year: equal, greater, less, in_range. Text fields: contains, equal, starts_with ...")
    value: str = Field(description="Value. For mytag: 'Column/Tag' (e.g. 'Energy/Peak'). For bpm: e.g. '120'.")
    value2: Optional[str] = Field(default=None, description="Upper bound for in_range")


def build_conditions(db: Rekordbox6Database, conds: list[SmartCondition]) -> list[dict]:
    out = []
    for c in conds:
        if c.op not in VALID_OPS[c.field]:
            raise ValueError(f"'{c.op}' cannot be used with {c.field}. Valid: {sorted(VALID_OPS[c.field])}")
        if c.op == "in_range" and not c.value2:
            raise ValueError("in_range requires value2")
        left, right, unit, label = c.value, c.value2 or "", "", c.value
        if c.field == "mytag":
            tag = resolve_tag(db, c.value)
            left, label = tag.ID, tag_label(db, tag)
        elif c.field == "bpm":
            # Rekordbox stores BPM x100 and writes ValueUnit="0".
            left = str(int(round(float(c.value) * 100)))
            right = str(int(round(float(c.value2) * 100))) if c.value2 else ""
            unit = "0"
        out.append({"field": c.field, "op": c.op, "left": left, "right": right, "unit": unit,
                    "label": f"{c.field} {c.op} {label}" + (f"..{c.value2}" if c.value2 else "")})
    return out


def smart_xml(playlist_id: str, conds: list[dict], match: str) -> str:
    pid = int(playlist_id)
    sid = pid - 2**32 if pid >= 2**31 else pid
    lo = 1 if match == "all" else 2

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")

    parts = [f'<NODE Id="{sid}" LogicalOperator="{lo}" AutomaticUpdate="0">']
    for c in conds:
        parts.append(
            f'<CONDITION PropertyName="{XML_PROP[c["field"]]}" Operator="{OP_CODES[c["op"]]}" '
            f'ValueUnit="{c["unit"]}" ValueLeft="{esc(c["left"])}" ValueRight="{esc(c["right"])}"/>'
        )
    parts.append("</NODE>")
    return "".join(parts)


def smart_match_count(db: Rekordbox6Database, conds: list[dict], match: str) -> int:
    C, S = tables.DjmdContent, tables.DjmdSongMyTag
    clauses = []
    for c in conds:
        f, op, left, right = c["field"], c["op"], c["left"], c["right"]
        if f == "mytag":
            sub = live(db.session.query(S.ContentID), S).filter(S.MyTagID == left)
            clauses.append(C.ID.in_(sub) if op == "contains" else ~C.ID.in_(sub))
            continue
        col = {"bpm": C.BPM, "year": C.ReleaseYear}.get(f)
        if col is not None:
            a = int(left)
            b = int(right) if right else None
            clauses.append({"equal": col == a, "not_equal": col != a, "greater": col > a, "less": col < a,
                            "in_range": and_(col >= a, col <= (b or a))}[op])
            continue
        if f == "title":
            col = C.Title
        elif f == "genre":
            col = tables.DjmdGenre.Name
        else:
            col = tables.DjmdArtist.Name
        v = left.lower()
        expr = {"equal": func.lower(col) == v, "not_equal": func.lower(col) != v,
                "contains": func.lower(col).contains(v), "not_contains": ~func.lower(col).contains(v),
                "starts_with": func.lower(col).startswith(v), "ends_with": func.lower(col).endswith(v)}[op]
        if f == "genre":
            expr = C.GenreID.in_(db.session.query(tables.DjmdGenre.ID).filter(expr))
        elif f == "artist":
            expr = C.ArtistID.in_(db.session.query(tables.DjmdArtist.ID).filter(expr))
        clauses.append(expr)
    q = live(db.session.query(C), C)
    if clauses:
        q = q.filter(and_(*clauses) if match == "all" else or_(*clauses))
    return q.count()


# --------------------------------------------------------------------------- pending actions

PENDING: dict[str, dict] = {}


def stage(kind: str, summary: str, params: dict, details: Optional[dict] = None) -> dict:
    aid = secrets.token_hex(4)
    PENDING[aid] = {"kind": kind, "summary": summary, "params": params}
    out = {"action_id": aid, "preview": summary}
    if details:
        out.update(details)
    out["next"] = "Nothing has been written yet. Show the preview to the user and call rb_commit with the action_ids once the user approves."
    if not WRITES_ENABLED:
        out["note"] = "Writes are disabled (set RB_ENABLE_WRITES=1)."
    return out


def _hex(pid: str) -> str:
    return "0" if pid in ("root", "", None) else format(int(pid), "X")


def update_master_xml(ops: list) -> None:
    path = DB_DIR / "masterPlaylists6.xml"
    if not ops or not path.exists():
        return
    with open(path, encoding="utf-8", newline="") as fh:  # keep CRLF line endings
        xml = fh.read()
    nl = "\r\n" if "\r\n" in xml else "\n"
    ts = str(int(_dt.datetime.now().timestamp() * 1000))

    def node_re(hid: str) -> re.Pattern:
        return re.compile(r'(<NODE Id="' + re.escape(hid) + r'"[^>]*/>)')

    for op in ops:
        if op[0] == "add":
            _, pid, parent, attr = op
            hid = _hex(pid)
            if node_re(hid).search(xml):
                continue
            line = (f'<NODE Id="{hid}" ParentId="{_hex(parent)}" Attribute="{attr}" '
                    f'Timestamp="{ts}" Lib_Type="0" CheckType="0"/>')
            m = re.search(r'\r?\n([ \t]*)</PLAYLISTS>', xml)
            indent = (m.group(1) if m else "  ") * 2
            xml = re.sub(r'(\r?\n[ \t]*</PLAYLISTS>)', lambda mm: nl + indent + line + mm.group(1), xml, count=1)
        elif op[0] in ("move", "touch"):
            hid = _hex(op[1])
            mm = node_re(hid).search(xml)
            if not mm:
                continue
            node = mm.group(1)
            new = re.sub(r'Timestamp="\d+"', f'Timestamp="{ts}"', node)
            if op[0] == "move":
                new = re.sub(r'ParentId="[0-9A-Fa-f]+"', f'ParentId="{_hex(op[2])}"', new)
            xml = xml.replace(node, new)
    tmp = path.with_suffix(".xml.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(xml)
    tmp.replace(path)


def backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / stamp
    dest.mkdir()
    for name in ("master.db", "master.db-wal", "master.db-shm", "masterPlaylists6.xml"):
        src = DB_DIR / name
        if src.exists():
            shutil.copy2(src, dest / name)
    olds = sorted(p for p in BACKUP_DIR.iterdir() if p.is_dir())
    for old in olds[:-BACKUP_KEEP]:
        shutil.rmtree(old, ignore_errors=True)
    return dest


def add_cloud_filter(db: Rekordbox6Database, pl: tables.DjmdPlaylist) -> None:
    # Rekordbox adds a djmdCloudFilterPlaylist row for playlists it creates itself.
    # pyrekordbox has no model for this table, so use SQL. Same values Rekordbox 7 writes.
    s = db.session
    if not s.execute(text("select count(*) from sqlite_master where name='djmdCloudFilterPlaylist'")).scalar():
        return
    while True:
        new_id = str(int.from_bytes(secrets.token_bytes(4), "big"))
        if not s.execute(text("select 1 from djmdCloudFilterPlaylist where ID=:i"), {"i": new_id}).first():
            break
    usn = db.registry.get_local_update_count() + 1
    db.registry.set_local_update_count(usn)
    n = utcnow()
    ts = n.strftime("%Y-%m-%d %H:%M:%S.") + f"{n.microsecond // 1000:03d} +00:00"
    s.execute(
        text("insert into djmdCloudFilterPlaylist (ID, PlaylistUUID, Seq, ParentID, UUID, rb_data_status, "
             "rb_local_data_status, rb_local_deleted, rb_local_synced, usn, rb_local_usn, created_at, updated_at) "
             "values (:id, :pu, 0, NULL, :u, 0, 0, 0, 0, NULL, :usn, :ts, :ts)"),
        {"id": new_id, "pu": pl.UUID, "u": new_uuid(), "usn": usn, "ts": ts},
    )


def create_playlist_row(db: Rekordbox6Database, name: str, parent_id: str, kind: str, xml_conds: Optional[tuple] = None) -> tables.DjmdPlaylist:
    parent = None if parent_id == "root" else parent_id
    if kind == "folder":
        pl = db.create_playlist_folder(name, parent=parent)
    elif kind == "smart":
        from pyrekordbox.db6.smartlist import SmartList
        sl = SmartList()  # placeholder, replaced with Rekordbox-compatible XML below
        pl = db.create_smart_playlist(name, sl, parent=parent)
        conds, match = xml_conds  # type: ignore[misc]
        pl.SmartList = smart_xml(pl.ID, conds, match)
    else:
        pl = db.create_playlist(name, parent=parent)
    add_cloud_filter(db, pl)
    return pl


def apply(db: Rekordbox6Database, kind: str, p: dict, xml_ops: list) -> str:
    S, T, SP = tables.DjmdSongMyTag, tables.DjmdMyTag, tables.DjmdSongPlaylist
    now = _dt.datetime.now()  # pyrekordbox converts local time to UTC when saving
    if kind in ("create_folder", "create_playlist", "create_smart_playlist"):
        parent_id = resolve_parent(db, p.get("parent"))
        if kind == "create_smart_playlist":
            conds = build_conditions(db, [SmartCondition(**c) for c in p["conditions"]])
            pl = create_playlist_row(db, p["name"], parent_id, "smart", (conds, p["match"]))
        else:
            pl = create_playlist_row(db, p["name"], parent_id, "folder" if kind == "create_folder" else "playlist")
        xml_ops.append(("add", pl.ID, parent_id, pl.Attribute))
        if kind == "create_playlist" and p.get("content_ids"):
            for cid in p["content_ids"]:
                db.add_to_playlist(pl, cid)
        return f"Created '{p['name']}' (id {pl.ID})"
    if kind == "add_tracks":
        pl = resolve_playlist(db, p["playlist"], folder=False)
        existing = {r.ContentID for r in live(db.session.query(SP), SP).filter(SP.PlaylistID == pl.ID)}
        n = 0
        for cid in p["content_ids"]:
            if cid not in existing:
                db.add_to_playlist(pl, cid)
                n += 1
        xml_ops.append(("touch", pl.ID))
        return f"Added {n} tracks to '{pl.Name}'"
    if kind == "remove_tracks":
        pl = resolve_playlist(db, p["playlist"], folder=False)
        rows = live(db.session.query(SP), SP).filter(SP.PlaylistID == pl.ID, SP.ContentID.in_(p["content_ids"])).all()
        for r in rows:
            db.remove_from_playlist(pl, r)
        xml_ops.append(("touch", pl.ID))
        return f"Removed {len(rows)} tracks from '{pl.Name}'"
    if kind == "edit_playlist":
        pl = resolve_playlist(db, p["playlist"])
        msgs = []
        if p.get("new_name"):
            db.rename_playlist(pl, p["new_name"])
            msgs.append(f"renamed to '{p['new_name']}'")
        if p.get("new_parent") is not None:
            parent_id = resolve_parent(db, p["new_parent"])
            db.move_playlist(pl, None if parent_id == "root" else parent_id)
            xml_ops.append(("move", pl.ID, parent_id))
            msgs.append("moved")
        xml_ops.append(("touch", pl.ID))
        return f"'{pl.Name}': " + ", ".join(msgs)
    if kind == "create_mytag":
        col = resolve_column(db, p["column"])
        seq = live(db.session.query(T), T).filter(T.ParentID == col.ID).count() + 1
        tag = T(ID=unused_id(db, T), Seq=seq, Name="New Tag", Attribute=0, ParentID=col.ID,
                UUID=new_uuid(), created_at=now, updated_at=now)
        db.add(tag)
        tag.Name = p["name"]  # same two-step as Rekordbox (create, then rename)
        return f"Created MyTag {col.Name}/{p['name']} (id {tag.ID})"
    if kind == "rename_mytag":
        target = resolve_column(db, p["target"]) if p["is_column"] else resolve_tag(db, p["target"])
        old = target.Name
        target.Name = p["new_name"]
        return f"Renamed '{old}' to '{p['new_name']}'"
    if kind in ("tag_tracks", "untag_tracks"):
        tag = resolve_tag(db, p["tag"])
        existing = {r.ContentID: r for r in live(db.session.query(S), S).filter(S.MyTagID == tag.ID)}
        n = 0
        for cid in p["content_ids"]:
            if kind == "tag_tracks" and cid not in existing:
                db.add(S(ID=new_uuid(), MyTagID=tag.ID, ContentID=cid, TrackNo=None, UUID=new_uuid(),
                         created_at=now, updated_at=now))
                n += 1
            elif kind == "untag_tracks" and cid in existing:
                db.delete(existing[cid])
                n += 1
        verb = "Tagged" if kind == "tag_tracks" else "Untagged"
        return f"{verb} {n} tracks with {tag_label(db, tag)}"
    raise ValueError(f"Unknown action {kind}")


# --------------------------------------------------------------------------- read tools


@mcp.tool()
def rb_status() -> dict:
    """Status: database path, whether Rekordbox is running, whether writes are enabled, track/playlist counts and pending actions."""
    with open_db() as db:
        C = tables.DjmdContent
        by_type = dict(live(db.session.query(C.FileType, func.count()), C).group_by(C.FileType).all())
        return {
            "db": str(DB_PATH),
            "rekordbox_running": rekordbox_running(),
            "writes_enabled": WRITES_ENABLED,
            "backup_dir": str(BACKUP_DIR),
            "tracks": {FILE_TYPES.get(k, str(k)): v for k, v in by_type.items()},
            "playlists": len(playlists(db)),
            "pending_actions": {k: v["summary"] for k, v in PENDING.items()},
        }


@mcp.tool()
def rb_list_playlists(folder: Optional[str] = None) -> str:
    """Show the playlist tree (folders, playlists, smart playlists) with ids and track counts. Optionally only below one folder."""
    with open_db() as db:
        pls = playlists(db)
        by_id = {p.ID: p for p in pls}
        counts = dict(
            live(db.session.query(tables.DjmdSongPlaylist.PlaylistID, func.count()), tables.DjmdSongPlaylist)
            .group_by(tables.DjmdSongPlaylist.PlaylistID).all()
        )
        children: dict[str, list] = {}
        for p in pls:
            children.setdefault(p.ParentID or "root", []).append(p)
        start = resolve_playlist(db, folder, folder=True).ID if folder else "root"
        lines: list[str] = []

        def walk(pid: str, depth: int) -> None:
            for p in sorted(children.get(pid, []), key=lambda x: x.Seq or 0):
                if p.Attribute == 1:
                    lines.append(f"{'  ' * depth}[folder] {p.Name}  (id {p.ID})")
                    walk(p.ID, depth + 1)
                elif p.Attribute == 4:
                    lines.append(f"{'  ' * depth}[smart] {p.Name}  (id {p.ID})")
                else:
                    lines.append(f"{'  ' * depth}{p.Name}  ({counts.get(p.ID, 0)} tracks, id {p.ID})")

        walk(start, 0)
        return "\n".join(lines) or "(tom)"


@mcp.tool()
def rb_get_playlist_tracks(playlist: str, limit: int = 500) -> dict:
    """Tracks in a playlist (id, full path 'Folder/Playlist' or unique name). Smart playlists return their conditions."""
    with open_db() as db:
        pl = resolve_playlist(db, playlist, folder=False)
        if pl.Attribute == 4:
            return {"playlist": pl.Name, "smart": True, "conditions_xml": pl.SmartList}
        SP = tables.DjmdSongPlaylist
        rows = live(db.session.query(SP), SP).filter(SP.PlaylistID == pl.ID).order_by(SP.TrackNo).limit(limit).all()
        ids = [r.ContentID for r in rows]
        tags = tags_for(db, ids)
        tracks = [track_dict(r.Content, tags.get(r.ContentID, [])) for r in rows if r.Content]
        return {"playlist": pl.Name, "id": pl.ID, "count": len(tracks), "tracks": tracks}


@mcp.tool()
def rb_search_tracks(
    query: Optional[str] = None,
    bpm_min: Optional[float] = None,
    bpm_max: Optional[float] = None,
    key: Optional[str] = None,
    mytag: Optional[str] = None,
    untagged_column: Optional[str] = None,
    genre: Optional[str] = None,
    source: Optional[Literal["tidal", "local", "spotify", "apple-music"]] = None,
    limit: int = 100,
) -> dict:
    """Search the Collection. query matches title/artist. key e.g. '8A' or 'Am'. mytag e.g. 'Energy/Peak'.
    untagged_column: only tracks without any tag in that MyTag column (e.g. 'Energy')."""
    with open_db() as db:
        C = tables.DjmdContent
        q = live(db.session.query(C), C)
        if query:
            like = f"%{query.lower()}%"
            q = q.outerjoin(tables.DjmdArtist, C.ArtistID == tables.DjmdArtist.ID).filter(
                or_(func.lower(C.Title).like(like), func.lower(tables.DjmdArtist.Name).like(like)))
        if bpm_min is not None:
            q = q.filter(C.BPM >= int(bpm_min * 100))
        if bpm_max is not None:
            q = q.filter(C.BPM <= int(bpm_max * 100))
        if key:
            q = q.join(tables.DjmdKey, C.KeyID == tables.DjmdKey.ID).filter(func.lower(tables.DjmdKey.ScaleName) == key.lower())
        if genre:
            q = q.join(tables.DjmdGenre, C.GenreID == tables.DjmdGenre.ID).filter(func.lower(tables.DjmdGenre.Name).contains(genre.lower()))
        S = tables.DjmdSongMyTag
        if mytag:
            tag = resolve_tag(db, mytag)
            q = q.filter(C.ID.in_(live(db.session.query(S.ContentID), S).filter(S.MyTagID == tag.ID)))
        if untagged_column:
            col = resolve_column(db, untagged_column)
            tag_ids = db.session.query(tables.DjmdMyTag.ID).filter(tables.DjmdMyTag.ParentID == col.ID)
            q = q.filter(~C.ID.in_(live(db.session.query(S.ContentID), S).filter(S.MyTagID.in_(tag_ids))))
        if source == "tidal":
            q = q.filter(C.FileType == 22)
        elif source == "spotify":
            q = q.filter(C.FileType == 25)
        elif source == "apple-music":
            q = q.filter(C.FileType == 26)
        elif source == "local":
            q = q.filter(C.FileType.notin_([22, 25, 26]))
        total = q.count()
        rows = q.limit(limit).all()
        tags = tags_for(db, [r.ID for r in rows])
        return {"total": total, "returned": len(rows), "tracks": [track_dict(r, tags.get(r.ID, [])) for r in rows]}


@mcp.tool()
def rb_tracks_by_tidal_ids(tidal_ids: list[str]) -> dict:
    """Look up Tidal track ids in the Rekordbox Collection. Returns the Rekordbox tracks and which Tidal ids are missing."""
    with open_db() as db:
        tracks, missing = resolve_tracks(db, None, tidal_ids)
        tags = tags_for(db, [t.ID for t in tracks])
        return {"found": len(tracks), "missing": missing, "tracks": [track_dict(t, tags.get(t.ID, [])) for t in tracks]}


@mcp.tool()
def rb_list_mytags() -> dict:
    """All MyTag columns and tags with the number of tracks per tag."""
    with open_db() as db:
        S, T = tables.DjmdSongMyTag, tables.DjmdMyTag
        counts = dict(live(db.session.query(S.MyTagID, func.count()), S).group_by(S.MyTagID).all())
        out = {}
        for col in mytag_columns(db):
            tags = live(db.session.query(T), T).filter(T.ParentID == col.ID).order_by(T.Seq).all()
            out[col.Name] = [{"tag": t.Name, "id": t.ID, "tracks": counts.get(t.ID, 0)} for t in tags]
        return out


# --------------------------------------------------------------------------- preview tools


@mcp.tool()
def rb_preview_create_folder(name: str, parent: Optional[str] = None) -> dict:
    """Preview: create a playlist folder (parent = folder path/id, empty = top level)."""
    with open_db() as db:
        resolve_parent(db, parent)
    return stage("create_folder", f"Create folder '{name}' in {parent or 'top level'}", {"name": name, "parent": parent})


@mcp.tool()
def rb_preview_create_playlist(name: str, parent: Optional[str] = None, track_ids: Optional[list[str]] = None,
                               tidal_ids: Optional[list[str]] = None) -> dict:
    """Preview: create a regular playlist, optionally with tracks (Rekordbox ids and/or Tidal ids)."""
    with open_db() as db:
        resolve_parent(db, parent)
        tracks, missing = resolve_tracks(db, track_ids, tidal_ids)
    return stage("create_playlist", f"Create playlist '{name}' in {parent or 'top level'} with {len(tracks)} tracks",
                 {"name": name, "parent": parent, "content_ids": [t.ID for t in tracks]},
                 {"missing": missing} if missing else None)


@mcp.tool()
def rb_preview_create_smart_playlist(name: str, conditions: list[SmartCondition], match: Literal["all", "any"] = "all",
                                     parent: Optional[str] = None) -> dict:
    """Preview: create a smart playlist. match='all' (every condition) or 'any' (at least one).
    Fields: mytag ('Column/Tag'), bpm, genre, artist, title, year. mytag and bpm are verified against Rekordbox 7."""
    with open_db() as db:
        resolve_parent(db, parent)
        built = build_conditions(db, conditions)
        n = smart_match_count(db, built, match)
    unverified = sorted({c.field for c in conditions} - VERIFIED_FIELDS)
    details: dict = {"matches_now": n, "conditions": [c["label"] for c in built]}
    if unverified:
        details["warning"] = f"Fields {unverified} are not verified against Rekordbox yet. Check the playlist in Rekordbox after committing."
    return stage("create_smart_playlist", f"Create smart playlist '{name}' ({match}: " + "; ".join(c["label"] for c in built) + f") in {parent or 'top level'}, currently matches {n} tracks",
                 {"name": name, "parent": parent, "match": match, "conditions": [c.model_dump() for c in conditions]}, details)


@mcp.tool()
def rb_preview_add_tracks(playlist: str, track_ids: Optional[list[str]] = None, tidal_ids: Optional[list[str]] = None) -> dict:
    """Preview: add tracks to an existing playlist. Duplicates are skipped."""
    with open_db() as db:
        pl = resolve_playlist(db, playlist, folder=False)
        if pl.Attribute == 4:
            raise ValueError("Cannot add tracks to a smart playlist")
        tracks, missing = resolve_tracks(db, track_ids, tidal_ids)
        SP = tables.DjmdSongPlaylist
        existing = {r.ContentID for r in live(db.session.query(SP), SP).filter(SP.PlaylistID == pl.ID)}
        new = [t for t in tracks if t.ID not in existing]
    return stage("add_tracks", f"Add {len(new)} tracks to '{pl.Name}' ({len(tracks) - len(new)} already there)",
                 {"playlist": pl.ID, "content_ids": [t.ID for t in new]}, {"missing": missing} if missing else None)


@mcp.tool()
def rb_preview_remove_tracks(playlist: str, track_ids: Optional[list[str]] = None, tidal_ids: Optional[list[str]] = None) -> dict:
    """Preview: remove tracks from a playlist (they stay in the Collection)."""
    with open_db() as db:
        pl = resolve_playlist(db, playlist, folder=False)
        tracks, missing = resolve_tracks(db, track_ids, tidal_ids)
    return stage("remove_tracks", f"Remove {len(tracks)} tracks from '{pl.Name}' (they stay in the Collection)",
                 {"playlist": pl.ID, "content_ids": [t.ID for t in tracks]}, {"missing": missing} if missing else None)


@mcp.tool()
def rb_preview_edit_playlist(playlist: str, new_name: Optional[str] = None, new_parent: Optional[str] = None) -> dict:
    """Preview: rename and/or move a playlist or folder. new_parent='root' moves it to the top level."""
    if not new_name and new_parent is None:
        raise ValueError("Provide new_name and/or new_parent")
    with open_db() as db:
        pl = resolve_playlist(db, playlist)
        if new_parent is not None:
            resolve_parent(db, new_parent)
    bits = ([f"rename to '{new_name}'"] if new_name else []) + ([f"move to {new_parent}"] if new_parent is not None else [])
    return stage("edit_playlist", f"'{pl.Name}': " + ", ".join(bits), {"playlist": pl.ID, "new_name": new_name, "new_parent": new_parent})


@mcp.tool()
def rb_preview_create_mytag(column: str, name: str) -> dict:
    """Preview: create a new MyTag in an existing column (e.g. column='Energy', name='Opener')."""
    with open_db() as db:
        col = resolve_column(db, column)
        T = tables.DjmdMyTag
        if live(db.session.query(T), T).filter(T.ParentID == col.ID, func.lower(T.Name) == name.lower()).count():
            raise ValueError(f"{col.Name}/{name} already exists")
    return stage("create_mytag", f"Create MyTag {col.Name}/{name}", {"column": col.ID, "name": name})


@mcp.tool()
def rb_preview_rename_mytag(target: str, new_name: str, is_column: bool = False) -> dict:
    """Preview: rename a MyTag ('Column/Tag') or a column (is_column=True, e.g. 'Untitled Column')."""
    with open_db() as db:
        t = resolve_column(db, target) if is_column else resolve_tag(db, target)
    return stage("rename_mytag", f"Rename {'column' if is_column else 'MyTag'} '{t.Name}' to '{new_name}'",
                 {"target": t.ID, "is_column": is_column, "new_name": new_name})


@mcp.tool()
def rb_preview_tag_tracks(tag: str, track_ids: Optional[list[str]] = None, tidal_ids: Optional[list[str]] = None) -> dict:
    """Preview: add a MyTag ('Column/Tag') to tracks. Tracks that already have it are skipped."""
    with open_db() as db:
        t = resolve_tag(db, tag)
        tracks, missing = resolve_tracks(db, track_ids, tidal_ids)
        S = tables.DjmdSongMyTag
        has = {r.ContentID for r in live(db.session.query(S), S).filter(S.MyTagID == t.ID)}
        new = [x for x in tracks if x.ID not in has]
        label = tag_label(db, t)
    return stage("tag_tracks", f"Tag {len(new)} tracks with {label} ({len(tracks) - len(new)} already have it)",
                 {"tag": t.ID, "content_ids": [x.ID for x in new]}, {"missing": missing} if missing else None)


@mcp.tool()
def rb_preview_untag_tracks(tag: str, track_ids: Optional[list[str]] = None, tidal_ids: Optional[list[str]] = None) -> dict:
    """Preview: remove a MyTag from tracks."""
    with open_db() as db:
        t = resolve_tag(db, tag)
        tracks, missing = resolve_tracks(db, track_ids, tidal_ids)
        label = tag_label(db, t)
    return stage("untag_tracks", f"Remove {label} from up to {len(tracks)} tracks",
                 {"tag": t.ID, "content_ids": [x.ID for x in tracks]}, {"missing": missing} if missing else None)


# --------------------------------------------------------------------------- commit


@mcp.tool()
def rb_discard(action_ids: Optional[list[str]] = None) -> str:
    """Discard pending actions (all of them if action_ids is empty)."""
    ids = action_ids or list(PENDING)
    for a in ids:
        PENDING.pop(a, None)
    return f"Discarded {len(ids)} actions"


@mcp.tool()
def rb_commit(action_ids: list[str]) -> dict:
    """Write approved actions to Rekordbox in the given order. Requires that the user approved the preview,
    RB_ENABLE_WRITES=1 and that Rekordbox is closed. Takes a backup first. All or nothing."""
    if not WRITES_ENABLED:
        return {"ok": False, "error": "Writes are disabled. Set RB_ENABLE_WRITES=1 in the MCP configuration."}
    if rekordbox_running():
        return {"ok": False, "error": "Rekordbox is running. Ask the user to quit Rekordbox and try again."}
    unknown = [a for a in action_ids if a not in PENDING]
    if unknown:
        return {"ok": False, "error": f"Unknown action_ids: {unknown}"}
    bdir = backup()
    results = []
    xml_ops: list = []
    with open_db() as db:
        # pyrekordbox would rewrite timestamps for every playlist in masterPlaylists6.xml;
        # we update only the nodes we touch, the way Rekordbox does.
        db.playlist_xml = None
        try:
            for a in action_ids:
                act = PENDING[a]
                results.append(apply(db, act["kind"], act["params"], xml_ops))
                db.session.flush()
            db.commit()
        except Exception as e:
            db.session.rollback()
            return {"ok": False, "error": f"Nothing was written: {e}", "backup": str(bdir)}
    try:
        update_master_xml(xml_ops)
    except Exception as e:  # DB is the source of truth in Rekordbox 6/7
        results.append(f"Warning: masterPlaylists6.xml was not updated ({e})")
    for a in action_ids:
        PENDING.pop(a, None)
    return {"ok": True, "done": results, "backup": str(bdir), "next": "The user can open Rekordbox again."}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
