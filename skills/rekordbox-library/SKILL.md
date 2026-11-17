---
name: rekordbox-library
description: Organise a Rekordbox 6/7 DJ library with the rekordbox-claude-mcp server - MyTags, smart playlists, folders, swapping track versions and keeping Tidal playlists in sync. Use whenever the user asks to sort, tag, clean up or build playlists in Rekordbox, or to mirror Rekordbox to Tidal.
---

# Rekordbox library work with rekordbox-claude-mcp

The server (`rb_*` tools) reads master.db and writes only through `rb_commit`. Every change is a preview first. Treat the user's library as irreplaceable: show what will happen, get a yes, then commit.

## Ground rules

- **Preview, show, commit.** Call `rb_preview_*`, show the user the preview text (counts, names), and call `rb_commit` only after they approve. Never commit on your own initiative.
- **Rekordbox must be closed** for commits. Check `rb_status` (`rekordbox_running`) before committing. If it is open, ask the user to quit it; do not retry in a loop.
- **Batch commits.** Collect related previews and commit them in one `rb_commit` so the user only quits Rekordbox once. Order matters: action ids run in the order given.
- **Two rounds when you create things you then use.** A preview cannot reference a MyTag, column or folder created in the same batch. Round 1: create the tag/folder and commit. Round 2: tag tracks or create smart playlists that use it.
- **A backup is taken before every commit.** Mention the backup folder in your summary. Restoring is described in the README.
- The server never deletes tracks or playlists. If the user wants something gone, tell them to do it in Rekordbox.

## Recommended structure

- **One MyTag column for genre** (e.g. `Genre/Deep House`) and **smart playlists per tag**, grouped in folders (e.g. `GENRE/ELECTRONIC/Deep House`). Smart playlists stay correct when tags change; regular playlists do not.
- Use other columns for cross-cutting facets: energy (`Energy/Warmup`, `Groove`, `Peak`), situation (`Situation/Party Banger`), or info flags (`Info/New`). Combine them with `match="all"` smart playlists, e.g. Chill + Deep House = "Lounge House".
- There is no tool to create a MyTag *column*. Reuse an unused column with `rb_preview_rename_mytag(is_column=True)` or ask the user to add one in Rekordbox.
- When a genre list grows past ~150 tracks, split it by sub-style (sound), not by year. Era only makes sense where the era is the genre (e.g. "Classic House 90s / 00s").
- A "new" list is easiest as a smart playlist on `date_added` `in_last` 30/60/90 days.

## Smart playlists

Fields: `mytag` ('Column/Tag'), `bpm`, `date_added` (`in_last`/`not_in_last`, unit `day` or `month`) are verified against XML written by Rekordbox 7. `genre`, `artist`, `title`, `year` use the same format but are unverified: tell the user to check the result in Rekordbox the first time. The preview reports `matches_now`; mention it.

## Getting tracks in (Tidal workflow)

Tracks must be in the Rekordbox Collection before they can be tagged. For streamed tracks:

1. Build a Tidal playlist with the tracks (e.g. via a Tidal MCP), prefixed so it is easy to find, e.g. `IMPORT - ...`.
2. The user imports it in Rekordbox (Tidal browser) and quits Rekordbox.
3. `rb_tracks_by_tidal_ids` to map Tidal ids to Rekordbox ids. **Read the full titles now.** Tidal search results often hide the mix name, so a long version may turn out to be a remix, dub or "(Mixed)" DJ-mix edit. Drop wrong ones before tagging.
4. Tag with `rb_preview_tag_tracks` and commit. The import playlist can then be deleted by the user.

## Versions

DJs usually want the **original, and the extended / 12" / original mix if it exists**. Flag radio edits, single edits, 7" edits, acoustic, live, karaoke, "in the style of", covers and `(Mixed)` DJ-mix versions (they have transitions baked in). `rb_search_tracks` with queries like `radio`, `edit`, `version`, `mixed`, `live`, `acoustic`, `karaoke` finds most of them; limit the report to tracks that carry tags.

To swap a version, import the new one and use `rb_preview_replace_tracks` with `old_track_id` + `new_track_id` (or `new_tidal_id`). Tags and regular-playlist positions move to the new version; the old one stays in the Collection without tags. Batch many pairs into one preview. If no better version exists, leave the old one and say so.

## Mirroring Rekordbox to Tidal

Rekordbox is the source of truth. To sync:

1. `rb_tags_export(column="Genre")` (plus `all_of=[...]` for combined lists) gives Tidal ids per tag.
2. For each Tidal playlist: clear it, then add the ids in one call. Re-check the track count afterwards: big lists sometimes need a second clear, and a list with unavailable tracks may not empty at all. Then create a fresh list and archive the old one.
3. Tracks without a Tidal id (local files, other services) are reported as `non_tidal` and cannot be mirrored. Tell the user.
4. Tidal apps show only one folder level. Never nest folders; archive playlists directly in an archive folder.

Tidal playlists are static, so the mirror must be re-run after tag changes.

## Reporting back

Keep the summary short: what changed (counts per list), what was skipped and why, the backup folder, and what the user needs to do next (open Rekordbox, delete import lists, check a list by ear).
