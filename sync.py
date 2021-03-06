from . import git, pijul
import asyncio
import hashlib
import os
import shlex
import chalk
import merge3
import datetime

handled_git_commits = []


async def run(cmd):
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL
    )
    try:
        stdout, _ = await proc.communicate()
    except asyncio.CancelledError:
        proc.terminate()
        return ""
    return stdout.decode()


def urlToPath(url):
    return "/tmp/" + hashlib.sha256(url.encode()).hexdigest()[:16]

async def pullGit(url):
    # Check whether we have the repo downloaded already
    path = urlToPath(url)
    if os.path.isdir(path):
        print(f"  Git: Fetching {url} to {path}...")
        await run(f"cd {path}; git fetch")
        for r in (await run(f"cd {path}; git branch -r")).split("\n"):
            r = r[2:]
            if r.startswith("origin/"):
                branch = r.split("/", 1)[1]
                if not branch.startswith("HEAD -> "):
                    await run(f"cd {path}; git checkout {branch}")
        print(chalk.green("  Done."))
    else:
        print(f"  Git: Cloning {url} to {path}...")
        await run(f"cd /tmp; git clone \"{url}\" {path}")
        print(chalk.green("  Done."))

async def pullPijul(url):
    # Check whether we have the repo downloaded already
    path = urlToPath(url)
    if os.path.isdir(path):
        print(f"  Pijul: Fetching {url} to {path}...")
        await run(f"cd {path}; pijul pull --all")
        print(chalk.green("  Done."))
    else:
        print(f"  Pijul: Cloning {url} to {path}...")
        await run(f"mkdir {path}; cd {path}; pijul init; pijul pull --set-default --set-remote origin \"{url}\" --all")
        print(chalk.green("  Done."))


async def presyncGitToPijul(git, pijul):
    print("  Collecting new Git commits...")
    commits = []
    for r in (await run(f"cd {git}; git for-each-ref --format '%(refname) %(objectname)'")).split("\n"):
        if r == "":
            continue
        ref, commit = r.split(" ")
        if ref.startswith("refs/heads/"):
            branch = ref.split("/", 2)[2]
            commits += [(commit, branch) for commit in await presyncGitToPijulCommit(git, pijul, commit, branch)]
    return commits

async def presyncGitToPijulCommit(git, pijul, commit, branch):
    # Check whether Pijul repo has this commit imported already
    r = await run(f"cd {pijul}; pijul log --grep 'Imported from Git commit {commit}' --hash-only --branch {branch}")
    for patch_id in r.split("\n"):
        patch_id = patch_id.split(":")[0]
        if len(patch_id) == 88:  # this is to avoid repository id to be treated as a patch
            desc = await run(f"cd {pijul}; pijul patch --description {patch_id}")
            if desc.strip() == f"Imported from Git commit {commit}":
                # Yay, exported to Pijul already
                return []
    if (commit, branch) in handled_git_commits:
        # Exported to Pijul already
        return []

    # Check whether this is an imported commit
    message_lines = (await run(f"cd {git}; git log -1 --format=%B {commit}")).split("\n")
    if any((line.startswith("Imported from Pijul patch ") for line in message_lines)):
        return []

    # Not imported, make sure all its parents are imported first
    r = await run(f"cd {git}; git show -s --pretty=%P {commit}")
    commits = []
    for parent in r.split():
        if parent != "":
            commits += await presyncGitToPijulCommit(git, pijul, parent, branch)
    return commits + [commit]


async def syncGitToPijul(git, pijul, presync):
    print("  Syncing Git -> Pijul...")
    for commit, branch in presync:
        await syncGitToPijulCommit(git, pijul, commit, branch)
    if presync != []:
        print("  Pushing...")
        await run(f"cd {pijul}; pijul push --all")

async def syncGitToPijulCommit(git, pijul, commit, branch):
    # Check whether Pijul repo has this commit imported already
    # Notice that this duplicates code from presyncGitToPijulCommit, however,
    # this additional check will stop the commits from being duplicated.
    r = await run(f"cd {pijul}; pijul log --grep 'Imported from Git commit {commit}' --hash-only --branch {branch}")
    for patch_id in r.split("\n"):
        patch_id = patch_id.split(":")[0]
        if len(patch_id) == 88:  # this is to avoid repository id to be treated as a patch
            desc = await run(f"cd {pijul}; pijul patch --description {patch_id}")
            if desc.strip() == f"Imported from Git commit {commit}":
                # Yay, exported to Pijul already
                return
    if (commit, branch) in handled_git_commits:
        # Exported to Pijul already
        return

    # Check whether we've already imported the commit as a patch, and we can
    # reuse it. For example, look at the following tree:
    #
    # D <- hotfix-123
    # |
    # C
    # |
    # B <- master
    # |
    # A
    #
    # In this case, we don't want to import A and B twice.
    r = await run(f"cd {pijul}; pijul log --grep 'Imported from Git commit {commit}' --hash-only")
    for patch_id in r.split("\n"):
        patch_id = patch_id.split(":")[0]
        if len(patch_id) == 88:  # this is to avoid repository id to be treated as a patch
            desc = await run(f"cd {pijul}; pijul patch --description {patch_id}")
            if desc.strip() == f"Imported from Git commit {commit}":
                # Okay, the patch is on another branch. So we apply it
                print(f"  Syncing commit {commit}: {message}...")
                await run(f"cd {pijul}; pijul apply {patch_id} --branch {branch}")
                print(chalk.green(f"  Done. Reapplied patch {patch_id}"))
                return

    # Sync the commit itself now
    author = (await run(f"cd {git}; git --no-pager show -s --format='%an <%ae>' {commit}")).strip()
    date = (await run(f"cd {git}; git log -1 -s --format=%ci {commit}")).strip()
    date = "T".join(date.split(" ", 1))
    desc = f"Imported from Git commit {commit}"
    message = (await run(f"cd {git}; git log -1 --format=%B {commit}")).split("\n")[0]

    print(f"  Syncing commit {commit}: {message}...")

    await run(f"cd {pijul}; pijul checkout {branch}")

    # For each changed file
    for file in (await run(f"cd {git}; git diff-tree --no-commit-id --name-only -r {commit}")).split("\n"):
        if file == "":
            continue

        await run(f"cd {git}; git checkout {commit}")
        try:
            with open(f"{git}/{file}") as f:
                theirs = f.readlines()
        except IOError:
            theirs = None
        await run(f"cd {git}; git checkout {commit}^")
        try:
            with open(f"{git}/{file}") as f:
                base = f.readlines()
        except IOError:
            base = None
        try:
            with open(f"{pijul}/{file}") as f:
                ours = f.readlines()
        except IOError:
            ours = None

        # Perform a 3-way merge
        if base is None and ours is None:
            # Assume file creation
            base = []
            ours = []
        elif base is None and ours is not None:
            # Assume file recreation
            if ours == theirs:
                # No changes
                continue
            else:
                # Conflict
                with open(f"{pijul}/{file}", "w") as f:
                    f.write("/*\n")
                    f.write(" * Notice by GitPijul proxy: this file was recreated on Git side (commit\n")
                    f.write(f" * {commit[:10]}...). The original (Pijul) version is shown below; make sure to fix\n")
                    f.write(" * the conflict yourself by merging the Git changes and remove this banner.\n")
                    f.write(" */\n")
                    f.write("".join(ours))
                    print(chalk.yellow(f"  Conflict: {file} recreated by Git with different contents"))
                continue
        elif base is not None and theirs is None:
            # Assume file deletion
            os.unlink(f"{pijul}/{file}")
            continue
        elif base is not None and ours is None:
            # Deleted by us
            continue
        elif base == ours:
            with open(f"{pijul}/{file}", "w") as f:
                f.write("".join(theirs))
            continue
        elif base == theirs:
            with open(f"{pijul}/{file}", "w") as f:
                f.write("".join(ours))
            continue

        # Assume file modifications on Git side or both sides
        merge = merge3.Merge3(base, ours, theirs, is_cherrypick=True)
        for t in merge.merge_regions():
            if t[0] == "conflict":
                # Aw!..
                header = ""
                header += "/*\n"
                header += " * Notice by GitPijul proxy: this file was modified by both Git and Pijul. Make\n"
                header += " * sure to merge the conflict yourself and remove this banner.\n"
                header += " */\n"
                print(chalk.yellow(f"  Conflict: {file} modified by both Git and Pijul"))
                break
        else:
            # Yay! No conflicts
            header = ""

        merged = merge.merge_lines(
            name_a="Pijul",
            name_b=f"Git (commit {commit})",
            start_marker=">" * 32,
            mid_marker="=" * 32,
            end_marker="<" * 32
        )
        os.makedirs(os.path.dirname(f"{pijul}/{file}"), exist_ok=True)
        with open(f"{pijul}/{file}", "w") as f:
            f.write(header)
            f.write("".join(merged))

    # Check whether there are any changes
    if await run(f"cd {pijul}; pijul status --short") == "":
        print(chalk.yellow("  No changes (fast-forward)"))
        handled_git_commits.append((commit, branch))
        return

    # Record changes
    author = shlex.quote(author)
    message = shlex.quote(message)
    r = await run(f"cd {pijul}; pijul record --add-new-files --all --author {author} --branch {branch} --date '{date}' --description '{desc}' --message {message}")
    patch = r.replace("Recorded patch ", "").strip()

    print(chalk.green(f"  Done. Recorded patch {patch}"))


async def syncPijulToGit(git, pijul):
    print("  Syncing Pijul -> Git...")
    for r in (await run(f"cd {pijul}; pijul branches")).split("\n"):
        if r == "":
            continue

        branch = r[2:]
        await run(f"cd {git}; git checkout {branch}")


        # List patches that were exported to Git already
        r = await run(f"cd {git}; git log --grep='Imported from Pijul patch' --format='[Commit Boundary]%H %B'")
        exported = {}
        for part in r.split("[Commit Boundary]"):
            if part != "":
                commit, message = part.strip().split(" ", 1)
                for row in message.split("\n"):
                    if row.startswith("Imported from Pijul patch "):
                        patch_id = row.split()[-1]
                        break
                else:
                    continue
                if (commit, branch) not in handled_git_commits:
                    exported[patch_id] = commit

        # List Pijul patches
        r = (await run(f"cd {pijul}; pijul log --branch {branch}")).split("\n")

        i = 0
        pijul_patches = {}
        while i < len(r):
            if r[i] == "":
                i += 1
                continue

            # Hash
            patch_id = r[i].split(" ")[1].strip()
            i += 1
            # Internal id
            i += 1
            # Authors
            authors = r[i].split(" ", 1)[1].strip()
            i += 1
            # Timestamp
            timestamp = r[i].split(" ", 1)[1].strip()
            if "." in timestamp:
                timestamp = (
                    timestamp.split(".")[0] +  # 2019-05-26 14:52:37
                    "." +  # .
                    timestamp.split(".")[1][:6] +  # 697693
                    " " +  # space
                    timestamp.split(".")[1].split(" ", 1)[1]  # UTC
                )
            i += 1
            # Empty line
            i += 1
            # Message and description
            message = ""
            while i < len(r) and not r[i].startswith("\x1B[1mHash"):
                message += r[i][4:] + "\n"
                i += 1

            # Check whether this patch was actually imported from Git
            if any((line.startswith("Imported from Git commit ") for line in message.split("\n"))):
                continue

            pijul_patches[patch_id] = {
                "author": authors,
                "timestamp": timestamp,
                "message": message.strip()
            }


        # Generate pijul_patches->exported diff
        actions = []
        for patch_id, data in pijul_patches.items():
            if patch_id not in exported:
                # New patch
                actions.append({
                    "action": "add",
                    "patch_id": patch_id,
                    **data
                })
        for patch_id, commit in exported.items():
            if patch_id not in pijul_patches:
                # Revert patch
                actions.append({
                    "action": "remove",
                    "patch_id": patch_id,
                    **data
                })

        # Sort actions somehow
        actions.sort(key=lambda action: action["timestamp"])

        print("  Temporary reverting all changes...")
        for action in actions[::-1]:
            patch_id = action["patch_id"]
            if action["action"] == "add":
                await run(f"cd {pijul}; pijul unrecord {patch_id} --branch {branch}")
            elif action["action"] == "remove":
                await run(f"cd {pijul}; pijul apply {patch_id} --branch {branch}; pijul revert --all --branch {branch}")

        for action in actions:
            await syncPijulToGitPatch(branch, git, pijul, **action)

        if actions != []:
            print("  Pushing...")
            await run(f"cd {git}; git push")

async def syncPijulToGitPatch(branch, git, pijul, action, patch_id, author, timestamp, message):
    small_patch_id = patch_id[:10] + "..."
    if action == "add":
        print(f"  Syncing new patch {small_patch_id}: {message}")
        await run(f"cd {pijul}; pijul apply {patch_id} --branch {branch}")
    elif action == "remove":
        print(f"  Reverting patch {small_patch_id}: {message}")
        await run(f"cd {pijul}; pijul unrecord {patch_id} --branch {branch}")

    await run(f"cd {pijul}; pijul revert --all --branch {branch}")

    # Synchronize
    await run(f"rsync -rv -f'- .git/' -f'- .pijul/' {pijul}/ {git}/")

    # Commit
    is_empty = (await run(f"cd {git}; git status --short")).strip() == ""

    if action == "add":
        message = shlex.quote(f"{message}\n\nImported from Pijul patch {patch_id}")
    elif action == "remove":
        message = shlex.quote(f"{message}\n\nReverted Pijul patch {patch_id}")
    author = shlex.quote(author)
    date = str(timestamp)
    await run(f"cd {git}; git add --all; git commit --author={author} --date='{date}' --message={message} --no-edit --allow-empty")
    commit = (await run(f"cd {git}; git rev-parse HEAD")).strip()

    if is_empty:
        print(chalk.yellow(f"  No changes (fast-forward), committed {commit}"))
    else:
        print(chalk.green(f"  Done. Committed {commit}"))



async def sync(config):
    await pullGit(config["git"]["url"])
    await pullPijul(config["pijul"]["url"])
    presync = await presyncGitToPijul(urlToPath(config["git"]["url"]), urlToPath(config["pijul"]["url"]))
    await syncPijulToGit(urlToPath(config["git"]["url"]), urlToPath(config["pijul"]["url"]))
    await syncGitToPijul(urlToPath(config["git"]["url"]), urlToPath(config["pijul"]["url"]), presync)
    print(chalk.green(chalk.bold("  Sync complete!")))