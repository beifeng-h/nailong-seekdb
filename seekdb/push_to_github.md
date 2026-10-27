# Pushing to GitHub

The repository lives at `D:\数据库大赛` -- the `C:\Users\FOR FUTURE\Documents\ChatGPT\数据库大赛\seekdb`
path is a directory junction into it, so both point at the same files.

`origin` already points at <https://github.com/beifeng-h/nailong-seekdb.git>, and
`main` carries the deployment commit directly on top of the remote's signed
`Initial commit`, so the push is a plain fast-forward -- no force, no history
rewrite.

    git log --oneline --decorate -3
    d6ebc4f (HEAD -> main) Add SeekDB 1.3.0 deployment: scripts, telemetry ID and docs
    03d08a3 (origin/main) Initial commit

## 1. Push from your own terminal

    cd D:\数据库大赛
    git push origin main

Git Credential Manager opens a GitHub sign-in window the first time. Finish the
sign-in and the push completes.

## 2. Push with a personal access token

If no sign-in window appears, create a token at
<https://github.com/settings/tokens> with the `public_repo` scope, then either:

    git push https://<user>:<TOKEN>@github.com/beifeng-h/nailong-seekdb.git main

or store it once and push normally afterwards:

    git credential approve
    protocol=https
    host=github.com
    username=<user>
    password=<TOKEN>

    git push origin main

## TLS backend

The Codex sandbox cannot initialise the Windows TLS stack (schannel), so git
needs OpenSSL there. It is already configured for this repository:

    git config http.sslBackend openssl

In a normal terminal schannel works fine and the setting is harmless.

## What is and is not in the repository

* `seekdb/vendor/*.msi` is excluded. The installer is 150 MB and GitHub refuses
  git objects larger than 100 MB. Its download URL and SHA-256 are recorded in
  `seekdb/vendor/README.md`.
* `__pycache__` and editor droppings are excluded by `.gitignore`.
