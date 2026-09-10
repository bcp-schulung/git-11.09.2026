# Git Exercise: Branching and Merging

This guide demonstrates how to create branches, switch between them, and perform both fast-forward and non-fast-forward merges.

> Run each command block step-by-step in your terminal.

---

## 1. Check repository state and existing branches

Check the current working tree status.

```bash
git status
```

List the local branches. The branch you are currently on will be marked with an asterisk (`*`).

```bash
git branch
```

List all branches, including remote-tracking branches (if any exist).

```bash
git branch --all
```

---

## 2. Create and switch to a new branch

Create a new branch named `feature/first-merge`. This only creates the branch; it does not switch to it.

```bash
git branch feature/first-merge
```

Confirm the new branch now exists in the local branch list.

```bash
git branch --all
```

Switch to the `feature/first-merge` branch.

```bash
git checkout feature/first-merge
```

Verify that the active branch has changed.

```bash
git branch
```

---

## 3. Commit on the feature branch

Stage any changes to `README.md` made on this branch.

```bash
git add README.md
```

Commit the changes with a descriptive feature message.

```bash
git commit -m "feature: added a test to the readme"
```

---

## 4. Fast-forward merge back to master

Switch back to the `master` branch.

```bash
git checkout master
```

Merge `feature/first-merge` into `master`. Because `master` has not diverged, this will be a fast-forward merge.

```bash
git merge feature/first-merge
```

Review the commit history to see the linear result of the fast-forward merge.

```bash
git log
```

---

## 5. Create, work on, and merge a second feature branch

Create a new branch and switch to it in one step using `checkout -b`.

```bash
git checkout -b feature/second-merge
```

Stage and commit the new changes on this branch.

```bash
git add README.md
git commit -m "feature: added a test to the readme"
```

Switch back to `master`.

```bash
git checkout master
```

Merge `feature/second-merge` into `master` with `--no-ff` to force a merge commit, even if a fast-forward is possible. This preserves the branch history.

```bash
git merge feature/second-merge --no-ff
```

Inspect the history again. This time you should see a merge commit connecting the two branches.

```bash
git log
```