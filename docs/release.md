# 发布与版本约定

这份是给维护者看的。普通用户只需要看 [README](../README.md)。

## 版本号规则

**末位最多到 12，到顶就进位。`1.0.12` 之后是 `1.1.0`，不存在 `1.0.13`。**

```
1.0.1 → 1.0.2 → … → 1.0.12 → 1.1.0 → 1.1.1 → … → 1.1.12 → 1.2.0
```

不是技术限制，是免得版本号越滚越长（`1.0.47` 这种）。

发版时三处一起改：`pyproject.toml` 的 `version`、`bookforge/__init__.py` 的兜底值、
README 里的当前版本。镜像里的版本号由 `build-arg VERSION` 注入，不用手改代码。

`tests/test_v112.py::VersionSchemeTests` 会拦「顺手把 patch 递增到 13」——
碰到这条红就改 minor，**不要改测试**。

发布门禁里还有两道：末位 > 12 直接拒绝；git tag 必须和 `pyproject.toml` 里的一致
（防「打了 tag 忘了改源码」）。

## 镜像标签

```
:latest     最新正式版
:1.1.4      具体版本号，方便回滚
:dev        手动触发的开发构建
```

`:latest` **只跟正式 git tag（`push` 事件 + `refs/tags/vX.Y.Z`）走**。手动触发的构建
不管叫什么名字都不会覆盖它 —— 免得谁发个测试镜像，所有 `docker pull :latest` 的人
跟着拿到未发布的代码。

手动触发**只能填开发标签**（`dev`、`test-*`、`sha-*`、`pr-*`），其余一律拒绝 ——
包括正式版本号、`latest` 和 `candidate`。用白名单而不是黑名单，因为黑名单永远漏一个：
早先只拦 SemVer 形式，实测填 `latest` 就能把正式版覆盖掉。

## 连续推多个 tag 会怎样

GitHub Actions 的 `concurrency` 默认只保留「一个在跑 + 一个排队」，排队里再来的会挤掉
之前的。所以短时间内连续推 `v1.1.1` / `v1.1.2` / `v1.1.3`，**中间的版本可能被跳过** ——
`latest` 会是最后那版，但 `:1.1.1` 可能没有镜像。

这是**有意接受的取舍**，不是 bug：正常发布一次只发一个版本。真需要补发某个版本，
单独重跑那个 tag 的工作流即可（会走「只升不降」判断，不会把 `latest` 压回去）。

GitHub 官方支持 `concurrency.queue: max`，真需要「每个 tag 必有镜像」时加这一行即可
（注意不能和 `cancel-in-progress: true` 合用）。

## 发布流程

```
CI 绿 → 打 tag → push tag → 发布工作流自动跑
```

发布工作流会：

1. 先跑完整单元测试（`needs: test`，不绿就不进下一步）
2. 构建候选镜像，打 `candidate-<run_id>-<attempt>` 标签
3. 拉回候选镜像 → 起容器 → 校验 revision → 端到端冒烟 → 版本号断言
4. **全部通过后**，才 `docker tag` + `docker push` 正式版本号和 `latest`

所以「验的就是发的」—— 冒烟失败时 GHCR 上的 `latest` 和版本号都不会动。

`latest` 还有「只升不降」保护：晋升前会拉现有的 `:latest` 读它的版本号比较，
本次更低就跳过。如果读不出来（网络/权限故障），会 fail closed 而不是硬推。

## 本地构建

```bash
# 开发构建（版本号显示 dev）
docker compose up -d --build

# 部署正式版
EBOOKFORGE_VERSION=1.1.4 docker compose up -d --build
```
