# Git Exercise: Working with Submodules

This guide explains how to add, inspect, clone, and update Git submodules.
A submodule is a separate Git repository nested inside another Git repository.
It is useful when you want to include an external library or shared project
and pin it to a specific commit.

> Run each command block step-by-step in your terminal.

---

## 1. Check the repository state

Make sure you are inside the main repository and the working tree is clean.

```bash
cd /Users/bencoeppicus/Documents/git-11.09.2026
git status
```

List the existing submodules (there may be none yet).

```bash
git submodule status
```

---

## 2. Add a submodule

Add the `git-11.09.2026` repository as a submodule inside the `lib/` directory.

```bash
git submodule add https://github.com/bcp-schulung/git-11.09.2026.git lib/git-11.09.2026
```

Git creates two things:
- a new entry in `.gitmodules`
- a directory at `lib/git-11.09.2026` containing the cloned submodule

Inspect the generated `.gitmodules` file.

```bash
cat .gitmodules
```

Check the status of the main repository.

```bash
git status
```

You should see two new items staged for commit:
- `.gitmodules`
- `lib/git-11.09.2026`

---

## 3. Inspect the submodule contents

Change into the submodule directory and look around.

```bash
cd lib/git-11.09.2026
ls -la
```

Run `git status` inside the submodule. Notice that it is its own Git repository
with its own history.

```bash
git status
```

Return to the main repository root.

```bash
cd ../..
```

---

## 4. Commit the submodule

Stage and commit the submodule registration.

```bash
git add .gitmodules lib/git-11.09.2026
git commit -m "Add git-11.09.2026 as a submodule"
```

Verify the commit history.

```bash
git log --oneline -3
```

---

## 5. Clone a repository that already contains submodules

When you clone a project with submodules, the submodule directories are created
but they remain empty until you initialize and update them.

Open a second terminal and clone the repository into a temporary location.

```bash
cd /tmp
git clone /Users/bencoeppicus/Documents/git-11.09.2026 git-submodule-copy
```

Before initializing the submodules, check that the submodule directory is empty.

```bash
ls -la git-submodule-copy/lib/git-11.09.2026
```

Initialize and fetch all submodules.

```bash
cd git-submodule-copy
git submodule update --init --recursive
```

Now the submodule directory should contain files.

```bash
ls -la lib/git-11.09.2026
```

---

## 6. Update the submodule to a new commit

Change into the submodule and fetch the latest changes.

```bash
cd lib/git-11.09.2026
git fetch origin
```

Check which commit the submodule currently points to.

```bash
git log --oneline -1
```

Switch to the latest commit on the main branch.

```bash
git checkout origin/main
```

Return to the main repository. The submodule pointer is now updated and must be
committed in the parent repository.

```bash
cd ../..
git status
```

Stage and commit the updated submodule reference.

```bash
git add lib/git-11.09.2026
git commit -m "Update git-11.09.2026 submodule to latest commit"
```

---

## 7. Summary of useful commands

| Command | Purpose |
|---|---|
| `git submodule add <url> <path>` | Add a new submodule |
| `git submodule status` | List tracked submodules and their commits |
| `git submodule update --init --recursive` | Initialize and populate submodules |
| `git submodule update --remote` | Update submodules to the latest remote commit |
| `git submodule foreach '<command>'` | Run a shell command in every submodule |

---

## 8. Optional: Remove the submodule

If you want to undo the exercise, remove the submodule completely.

Remove the submodule from the index and delete the files.

```bash
git rm -f lib/git-11.09.2026
```

Remove the `.gitmodules` entry and clean the Git metadata.

```bash
rm -f .gitmodules
rm -rf .git/modules/lib/git-11.09.2026
```

Stage the deletions and commit.

```bash
git add -A
git commit -m "Remove git-11.09.2026 submodule"
```

