# HTB Writeup Fetcher

Download official HTB writeup PDFs in bulk.

```text
writeups/
├── htb_writeup.py
├── htb_challenge_writeups.py
├── htb_sherlock_writeups.py
├── machine/
├── challenge/
└── sherlocks/
```

```bash
python3 -m pip install requests

printf '%s' 'YOUR_HTB_TOKEN' > ~/.htb-token
chmod 600 ~/.htb-token

python3 writeups/htb_writeup.py --token-file ~/.htb-token
python3 writeups/htb_challenge_writeups.py --token-file ~/.htb-token
python3 writeups/htb_sherlock_writeups.py --token-file ~/.htb-token
```

Each script writes beside itself: Machines to `writeups/machine/`, Challenges to `writeups/challenge/`, and Sherlocks to `writeups/sherlocks/`.

Use `--limit 5` for a small test run. Existing PDFs are skipped locally; only missing PDFs are queued. Use `--force` to replace existing files.

Pass an exact name to download one item:

```bash
python3 writeups/htb_writeup.py Cap --token-file ~/.htb-token
```
