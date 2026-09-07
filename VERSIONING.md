# Versioning

Personal Tideway uses [PEP 440](https://peps.python.org/pep-0440/) versions.
The single source of truth is `personal_tideway.__version__`; package metadata
and `ptw --version` read from it.

- Development snapshots: `X.Y.Z.devN`
- Alpha, beta, and release candidates: `X.Y.ZaN`, `X.Y.ZbN`, `X.Y.ZrcN`
- Stable releases: `X.Y.Z`
- Git tags: `v` followed by the exact package version

Before a release:

1. update `personal_tideway.__version__`;
2. add matching bilingual notes to `CHANGELOG.md`;
3. run the complete tests and build;
4. inspect the repository for credentials and private paths;
5. commit, tag, and publish only after explicit approval.

## По-русски

Personal Tideway использует версии стандарта PEP 440. Единственный источник
версии — `personal_tideway.__version__`; из него берутся метаданные пакета и
вывод `ptw --version`.

Перед выпуском нужно обновить версию, добавить двуязычную запись в
`CHANGELOG.md`, выполнить все тесты и сборку, проверить репозиторий на секреты
и приватные пути и только после отдельного разрешения создавать commit, tag и
публикацию.
