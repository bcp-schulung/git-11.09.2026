# Git Exercise: First Repository

This guide walks through the basics of creating a local Git repository, configuring your identity, making commits, and inspecting history.

> Run each command block step-by-step in your terminal.

---

## 1. Create and initialize the project

Create a new directory for the project and move into it.

```bash
mkdir project
cd project
```

Initialize a fresh Git repository in the current directory.

```bash
git init
```

Check the repository status before making any changes.

```bash
git status
```

---

## 2. Add a README file and commit

Create an empty `README.md` file.

```bash
touch README.md
```

Stage the new file so Git starts tracking it.

```bash
git add README.md
```

Verify that `README.md` is staged and ready to be committed.

```bash
git status
```

Try to commit. The first attempt will fail if you have not yet configured your name and email.

```bash
git commit -m "chore: Added a readme file"
```

---

## 3. Configure Git user identity

Set your global email and name. Git needs this information to attribute commits to you.

```bash
git config --global user.email "ben.coeppicus@it-scholar.com"
git config --global user.name "Ben Cöppicus"
```

Now retry the commit. With your identity configured, it will succeed.

```bash
git commit -m "chore: Added a readme file"
```

---

## 4. Inspect the working directory and log

List the files in the project directory.

```bash
ll
```

Confirm that the working tree is clean after the commit.

```bash
git status
```

View the commit history.

```bash
git log
```

---

## 5. Make several more commits in a loop

Append three updates to `README.md`, staging and committing each one.

```bash
for i in {1..3}; do
  echo "Update $i" >> README.md
  git add README.md
  git commit -m "chore: Updated readme - iteration $i"
done
```

---

## 6. View the commit history

Display the commit history as a simple text graph.

```bash
git log --graph
```

Display a more colorful, concise history with abbreviations, relative timestamps, decorations, and all branches.

```bash
git log --graph --abbrev-commit --decorate --format=format:'%C(bold blue)%h%C(reset) - %C(bold green)(%ar)%C(reset) %C(white)%s%C(reset) %C(dim white)- %an%C(reset)%C(auto)%d%C(reset)' --all
```