# HTB Writeup Fetcher

Download the official PDF writeup for one retired Hack The Box machine.

```bash
python3 -m pip install requests
printf '%s' 'YOUR_HTB_TOKEN' > ~/.htb-token
chmod 600 ~/.htb-token
python3 htb_writeup.py Cap --token-file ~/.htb-token
```

Writeups are saved to `./writeups/` by default.

Requires access to the selected machine's official HTB writeup.
