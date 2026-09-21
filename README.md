# HTB Writeup Fetcher

Download official HTB writeup PDFs in bulk.

```bash
python3 -m pip install requests

printf '%s' 'YOUR_HTB_TOKEN' > ~/.htb-token
chmod 600 ~/.htb-token

python3 htb_writeup.py --token-file ~/.htb-token
python3 htb_challenge_writeups.py --token-file ~/.htb-token
python3 htb_sherlock_writeups.py --token-file ~/.htb-token
```

Use `--limit 5` for a small test run. Existing PDFs are skipped; use `--force` to replace them.

Pass an exact name to download one item, for example:

```bash
python3 htb_writeup.py Cap --token-file ~/.htb-token
```
