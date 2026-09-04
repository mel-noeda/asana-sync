# Release and Marketplace publish

Consumers pin `mel-noeda/asana-sync@v1`. Each public release is a semver tag (`v1.0.0`) plus a floating major tag (`v1`) that points at the same commit.

## Cut a release

1. Bump `version` in [`pyproject.toml`](pyproject.toml). Match the tag you will create (`1.0.0` → `v1.0.0`).
2. Commit on `main` and push.
3. Create an annotated tag and push it:

   ```bash
   git tag -a v1.0.0 -m "asana-sync v1.0.0"
   git push origin v1.0.0
   ```

4. Move the floating major tag to that commit:

   ```bash
   git tag -f v1 v1.0.0
   git push origin v1 --force
   ```

   Force-pushing `v1` is expected. Do not force-push `main` or a full semver tag that consumers already use.

5. Create the GitHub Release from that tag (see Marketplace below).

## Publish to the GitHub Marketplace

Marketplace is how the action appears in the GitHub **Actions** search UI.

1. Confirm the repository is **public**. Private actions can still be consumed with `uses: org/repo@tag` inside the org; they do not appear in the catalog.
2. Add a root `LICENSE` file if one is missing. GitHub requires a license for Marketplace listing (MIT is the usual choice for Actions).
3. Confirm root [`action.yml`](action.yml) has `name`, `description`, and `branding`.
4. On GitHub: **Releases → Draft a new release**.
5. Choose the semver tag (`v1.0.0`).
6. Check **Publish this Action to the GitHub Marketplace**.
7. Complete the listing form (primary category, optional another category, and a short Marketplace description).
8. Publish the release.

Later `v1.x.y` releases update the same listing when you publish them the same way. After each release, move `v1` as in step 4 above.

This is not a submission to [`github/starter-workflows`](https://github.com/actions/starter-workflows). Those templates power GitHub's curated "set up Pages" suggestions. Marketplace listing is the correct path for this action.

## After the first `v1` tag

Consuming repositories can add:

```yaml
- uses: mel-noeda/asana-sync@v1
```

or call a reusable workflow:

```yaml
jobs:
  sync:
    uses: mel-noeda/asana-sync/.github/workflows/reusable-asana-to-github.yml@v1
    secrets: inherit
```
