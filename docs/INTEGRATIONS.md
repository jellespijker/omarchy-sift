# Using Sift's tags from other programs

Sift stores tags the way the freedesktop world already does: in the extended attribute `user.xdg.tags` of the file, as a comma-separated
list of UTF-8 names. There is no Sift database to query, so any program that understands that attribute can use the tags, with or without Sift
installed.

```console
$ getfattr -n user.xdg.tags -e text /tmp/sift-demo/files/Projects/roadmap-q4.md
# file: /tmp/sift-demo/files/Projects/roadmap-q4.md
user.xdg.tags="meeting-note,meeting-notes"
```

(The file is from the demo data set. `sift demo on` creates it.)

## Dolphin and KDE (Baloo)

Dolphin shows tags through Baloo: `balooctl6 enable`, add your folders to Baloo's include list, then browse `tags:/` or search
`tag:transcript`. Sift never puts `/` in a tag name, because Dolphin treats it as a path separator.

## Scripts and the shell

```bash
getfattr -R -n user.xdg.tags -e text ~/Documents 2>/dev/null     # every tagged file
setfattr -n user.xdg.tags -v "invoice,2026" file.pdf              # tag by hand; Sift keeps tags it did not write
```

Sift merges with what is already on a file, so tags from Dolphin or a script survive. `sift tags scan` reads every tag on disk (whoever wrote
it), so the tag manager can rename, merge or delete them too.

## Keeping tags when you copy or back up

Extended attributes only travel when the tool asks for them: `cp --preserve=xattr`, `rsync -X`, `tar --xattrs`. Filesystems and sync tools differ
(whether Syncthing or a cloud drive keeps them is unverified here), so test your own set-up before relying on it. A filesystem without user xattrs
cannot hold tags at all; `sift doctor` checks each scanned folder.

## File managers without tag support

GNOME Files (Nautilus) does not show `user.xdg.tags` by default. A small `nautilus-python` column is possible but not shipped (unverified).

## Why a standard instead of our own index

- Your tags outlive the plugin, the model and the vendor.
- Every tool that already speaks the standard works on day one.
- Backups, `find`, scripts and other taggers need no adapter.
- The cost: tags live on the file, so they follow the file's filesystem and tools. Sift keeps its own change log (`sift untag`) so any rewrite can be undone.
