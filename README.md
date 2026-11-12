# Plane

Single-file HTML apps for the phone. Each app is one `.html`, no build step.
Served by GitHub Pages from `main`.

| App | File | What it does |
|---|---|---|
| Orbit Reader | `index.html` | Teleprompter-paced EPUB reader. Add books from Files, set words per minute, read at the eye line. Books live in the browser's IndexedDB. |
| busboi | `busboi/index.html` | Minutes until you walk out the door. Transit (Transitous/511 feed) from wherever the phone is to home, work or a theater, with the way back. |
| arrow | `arrow/index.html` | Driving routes in LA County that only turn left on a green arrow, with an avoid-hills switch; hands the route to Apple Maps. Routes on the phone; a GitHub Action rebuilds the LA roads monthly. |
| Bookbinder | `bookbinder/index.html` | Cleans old EPUBs and comics in bulk from Files. Books: chapters rebuilt from the book's own headings, junk pages cut, metadata filled, your cover, formatting tuned for Books on iPhone and iPad. Comics (CBZ/CBR): uploader and ad pages cut, the issue matched in the comic databases from its cover, barcode and indicia, out as CBZ with ComicInfo.xml, named and foldered for Panels. Claude key optional. |

## Install on iPhone
Open the Pages URL in Safari → Share → Add to Home Screen. Stay in the Home Screen app: it keeps its own book storage, separate from Safari.
