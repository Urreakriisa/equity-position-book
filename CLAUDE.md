# Working on this repository

- Increase `BUILD` in `app/build.py` by one in every change pushed to `main`. The app shows it in its footer and at `/healthz`.
- Run `python -m pytest -q` before pushing.
- Never commit positions, passwords or API keys. Positions live in the database; keys live in Railway variables.
- Railway deploys `main` automatically. The service must stay at one replica (the price refresh runs in-process).
