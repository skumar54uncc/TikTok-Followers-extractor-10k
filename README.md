# TikTok followers extractor

Downloads the public follower list for one TikTok profile into a CSV file.

Each row has:

- display name
- profile link
- follower count

It also tries the accounts that profile follows. TikTok only returns that list when the profile has made it public.

## Limit

TikTok’s public follower list stops after the most recent **~10,000** accounts. The profile can show a higher follower total, and this script cannot download past the point where TikTok ends the list. Run it again only to resume or refresh that public window.

Private profiles cannot be exported.

## Setup

Python 3.10 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows, activate with `.venv\Scripts\activate`.

## Run

```bash
python export_followers.py USERNAME
```

`USERNAME` is the TikTok handle, with or without `@`.

Example:

```bash
python export_followers.py someuser
```

The script writes files under `output/`:

| File | Contents |
| --- | --- |
| `output/<username>_followers.csv` | Followers |
| `output/<username>_following.csv` | Accounts they follow, when that list is public |
| `output/state.json` | Cursor so a stopped run can continue |
| `output/export.log` | Progress log |

These files stay on your machine. They are listed in `.gitignore`.

Progress prints in the terminal. A full public list is a few thousand requests and usually finishes in a few minutes. If you stop it, run the same command again and it continues from `output/state.json`.

## Start over

`output/state.json` belongs to the last username you exported. To export a different profile, or to download the public list again from the start, delete that file (and the CSV, if you want a new file):

```bash
rm -f output/state.json output/<username>_followers.csv
python export_followers.py USERNAME
```
