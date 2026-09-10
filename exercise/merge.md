# Git Exercise: Resolving a Merge Conflict

This guide intentionally creates a merge conflict so you can practice resolving it manually and completing the merge.

> Run each command block step-by-step in your terminal.

---

## 1. Create a conflicting change on a feature branch

Create and switch to a new branch named `feature/conflict`.

```bash
git checkout -b feature/conflict
```

Create a new file called `conflict.md` on the feature branch.

```bash
touch conflict.md
```

Stage the new file.

```bash
git add conflict.md
```

Commit it on the feature branch.

```bash
git commit -m "chore: made a conflict happen"
```

---

## 2. Create a conflicting change on master

Switch back to the `master` branch.

```bash
git checkout master
```

Create the same file on `master`, which will cause a conflict because both branches added `conflict.md` independently.

```bash
touch conflict.md
```

Stage the file on `master`.

```bash
git add conflict.md
```

Commit the change.

```bash
git commit -m "chore: made a conflict happen"
```

---

## 3. Trigger and resolve the merge conflict

Attempt to merge `feature/conflict` into `master` with `--no-ff`. Because both branches created `conflict.md` separately, Git will report a conflict.

```bash
git merge feature/conflict --no-ff
```

Open `conflict.md` in a text editor to resolve the conflict markers. In this exercise, the file is empty on both sides, so you can decide what the final content should be.

```bash
nano conflict.md
```

After editing, stage the resolved file.

```bash
git add conflict.md
```

Complete the merge by committing. Git will open an editor for the merge commit message; save and close it to finish.

```bash
git commit
```