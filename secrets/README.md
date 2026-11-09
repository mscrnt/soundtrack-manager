# secrets/

Optional secret files, mounted read-only at `/run/secrets` in the container.
Everything in this folder except this README is git-ignored.

| File                 | Used for                                   |
|----------------------|--------------------------------------------|
| `khinsider_password` | khinsider account password (with `KHINSIDER_USERNAME` in `.env`) |
| `web_password`       | Web UI password (required if `WEB_BIND` is not loopback) |

Each file holds just the value on one line. For example:

```sh
printf '%s' 'my-password' > secrets/khinsider_password
chmod 600 secrets/khinsider_password
```

Outside Docker, point to a file with `KHINSIDER_PASSWORD_FILE=/path/to/file`, or set
`STM_SECRETS_DIR` to this folder.
