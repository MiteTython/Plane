# Plane

Single-file HTML apps for the phone. Each app is one `.html`, no build step.
Served by GitHub Pages from `main`.

| App | File | What it does |
|---|---|---|
| Orbit Reader | `index.html` | Teleprompter-paced EPUB reader. Add books from Files, set words per minute, read at the eye line. Books live in the browser's IndexedDB. |
| busboi | `busboi/index.html` | Minutes until you walk out the door. Transit (Transitous/511 feed) from wherever the phone is to home, work or a theater, with the way back. |
| arrow | `arrow/index.html` | Driving routes in LA County that only turn left on a green arrow, with an avoid-hills switch; hands the route to Apple Maps. Routes on the phone; a GitHub Action rebuilds the LA roads monthly. |
| Bookbinder | `bookbinder/index.html` | Cleans old EPUBs in bulk from Files: chapters rebuilt from the book's own headings, junk pages cut, metadata filled, your cover, formatting tuned for Books on iPhone and iPad. Claude key optional. Out through the share sheet to Books or Files. |

## Install on iPhone
Open the Pages URL in Safari → Share → Add to Home Screen. Stay in the Home Screen app: it keeps its own book storage, separate from Safari.
