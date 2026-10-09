# rekordbox-claude-mcp

An [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP client) read your Rekordbox library and, with your approval, create **playlists, folders, smart playlists and MyTags**.

Built by a working DJ to organise a library of mostly streamed tracks (Tidal) with MyTags and smart playlists, without clicking through thousands of tracks by hand.

> **Unofficial.** Not affiliated with or endorsed by AlphaTheta or Pioneer DJ. It writes directly to Rekordbox's database. It takes a backup before every write, but use it at your own risk and keep your own backups too (File > Library > Backup Library).

## What you can ask for

- "Find everything between 120 and 124 BPM in 8A that has no Energy tag"
- "Create a MyTag column called Situation with Wedding, Corporate and Singalong"
- "Tag these 300 tracks as Genre/Nu Disco" (Rekordbox ids or Tidal ids)
- "Make smart playlists for Warmup, Groove and Peak based on my Energy tags"
- "Put these tracks in a new playlist under Events/Wedding"

## Safety model

- **Preview first.** Every change starts as a preview with an action id. Nothing is written until `rb_commit` is called with ids you approved.
- **Writes are off by default.** Set `RB_ENABLE_WRITES=1` when you are ready.
- **Rekordbox must be closed** to write. The server checks and refuses otherwise. Reading works while Rekordbox is open.
- **Backup before every write** of `master.db` (+ `-wal`/`-shm`) and `masterPlaylists6.xml`, into `rekordbox-claude-backups/<timestamp>/` next to your Rekordbox folder. The newest 20 are kept.
- **All or nothing.** A commit with several actions is written in one transaction.
- **No deletes.** It never deletes tracks from the Collection and never deletes playlists.

## Tools

| Read (Rekordbox may be open) | Preview (writes nothing) | Write |
|---|---|---|
| `rb_status` | `rb_preview_create_folder` | `rb_commit` |
| `rb_list_playlists` | `rb_preview_create_playlist` | `rb_discard` |
| `rb_get_playlist_tracks` | `rb_preview_create_smart_playlist` | |
| `rb_search_tracks` (text, BPM, key, MyTag, genre, source, untagged) | `rb_preview_add_tracks` / `rb_preview_remove_tracks` | |
| `rb_tracks_by_tidal_ids` | `rb_preview_edit_playlist` (rename/move) | |
| `rb_list_mytags` | `rb_preview_create_mytag` / `rb_preview_rename_mytag` | |
| | `rb_preview_tag_tracks` / `rb_preview_untag_tracks` | |

Playlists are referenced by id, full path (`01 - TOOLBOX/04 - PEAK`) or a unique name. MyTags as `Column/Tag` (`Energy/Peak`). Tracks by Rekordbox id or, for streamed tracks, Tidal id.

Smart playlist fields: `mytag`, `bpm`, `genre`, `artist`, `title`, `year`. **`mytag` and `bpm` are verified** against smart playlists created by Rekordbox 7 itself; the others use the same format but check the result in Rekordbox the first time you use them.

## Requirements

- Rekordbox 6 or 7 (tested on **Rekordbox 7 on macOS**; Windows and Rekordbox 6 should work but are untested)
- [uv](https://docs.astral.sh/uv/) (it installs Python and the dependencies for you)
- Claude Desktop or another MCP client

## Install

1. Install uv:
   - macOS: `curl -LsSf https://astral.sh/uv/install.sh | sh`
   - Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
2. Download or clone this repository, e.g. to `~/Projects/rekordbox-claude-mcp`.
3. Add the server to your MCP config. For Claude Desktop that is `claude_desktop_config.json` (Settings > Developer > Edit Config):

```json
{
  "mcpServers": {
    "rekordbox": {
      "command": "/Users/YOU/.local/bin/uv",
      "args": ["run", "--directory", "/Users/YOU/Projects/rekordbox-claude-mcp", "rekordbox-claude-mcp"],
      "env": {
        "PATH": "/Users/YOU/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "RB_ENABLE_WRITES": "0"
      }
    }
  }
}
```

   On Windows use the full path to `uv.exe` (usually `C:\Users\YOU\.local\bin\uv.exe`) and the Windows path to the folder.

4. Restart Claude Desktop. The first start takes up to a minute while uv downloads Python and the packages.
5. Ask Claude: "Run rb_status". When reading works, set `RB_ENABLE_WRITES` to `"1"` and restart.

### Settings

| Variable | Default | |
|---|---|---|
| `RB_ENABLE_WRITES` | off | `1` allows `rb_commit` |
| `RB_DB_DIR` | `~/Library/Pioneer/rekordbox` (macOS), `%APPDATA%\Pioneer\rekordbox` (Windows) | Folder with `master.db` |
| `RB_BACKUP_DIR` | `rekordbox-claude-backups` next to `RB_DB_DIR` | |
| `RB_BACKUP_KEEP` | `20` | Number of backups to keep |

## Workflow

1. Ask for a change. Claude shows you the preview.
2. Approve it and quit Rekordbox.
3. Claude commits. Open Rekordbox and check the result.

Batch related changes into one commit so you only have to quit Rekordbox once. Note that a preview cannot point at a folder created in the same batch: create the folder first, then its contents.

**Try it on something harmless first**, e.g. one smart playlist on the top level, and check it in Rekordbox before doing bulk tagging.

## Restoring a backup

Quit Rekordbox and copy the files from the backup folder back into your Rekordbox folder. If the backup has no `master.db-wal`/`master.db-shm`, delete those files in the Rekordbox folder first.

## How it works

Built on [pyrekordbox](https://github.com/dylanljones/pyrekordbox), which decrypts and edits Rekordbox's SQLCipher database. pyrekordbox does not write MyTags, so that part is written to match exactly what Rekordbox 7 writes, found by comparing the database before and after making the same changes in Rekordbox:

- MyTags live in `djmdMyTag` (columns under `root`, tags under their column) and `djmdSongMyTag` (one row per track and tag).
- Smart playlists store their rules as XML. `NODE Id` is the playlist id as a signed 32-bit integer, BPM values are stored ×100 with `ValueUnit="0"`, MyTag rules use the MyTag id.
- Rekordbox adds a `djmdCloudFilterPlaylist` row for new playlists; so does this server.
- `masterPlaylists6.xml` is updated only for the nodes that change (CRLF preserved). pyrekordbox's own XML sync is disabled because it rewrites timestamps on every playlist.
- Every changed row gets a new `rb_local_usn` from the local update counter, the same way Rekordbox does it.

## Development

```
uv run --with pytest pytest -q
```

The offline tests need no Rekordbox. To run the round-trip test, point it at a **copy** of a Rekordbox folder: `RB_TEST_DB_DIR=/path/to/copy uv run --with pytest pytest -q`.

## License

MIT. See [LICENSE](LICENSE). Rekordbox is a trademark of AlphaTheta Corporation.
