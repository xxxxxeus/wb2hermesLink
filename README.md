# wb2hermesLink (w2hlink)

**让现有 Hermes 使用你自己的 WorkBuddy 账号调用模型。** `w2hlink` 是一个 macOS 命令行接入工具：负责配置 provider、管理模型名称与上下文，以及登录认证；日常仍然在 Hermes 中使用模型。

无 GUI、本地代理、后台服务或新依赖，不捆绑 Python、Hermes 或 WorkBuddy 客户端。

版本 **v0.1.0**，MIT。仅本地验证过 **Hermes v0.21.3 (2026.9.14)、macOS 26.3.1 arm64**。不支持原生 Windows；Intel、其它 macOS、Linux/WSL 未验证，不承诺兼容。

### 接入方式

```text
         w2hlink install / models        w2hlink login
                    |                         |
                    v                         v
              Hermes config             Browser login
                    |                         |
                    |                         v
                    |                     Local auth
                    |                         |
                    |                  secrets.command
                    |                  (wblink.py env)
                    |                         |
                    +------------+------------+
                                 |
                                 v
                           Hermes Agent
                                 |
                           direct HTTPS
                                 |
                                 v
                           WorkBuddy API
                                 |
                                 v
                             hy3 / ...
```

- **配置和登录**：`w2hlink` 写入 WorkBuddy provider、模型与上下文配置；显式执行 `login` 时，通过浏览器完成授权，将认证保存在本机。
- **加载认证**：Hermes 启动时，通过原生 `secrets.command` 调用 `wblink.py env`，读取已保存的认证；不会自动打开登录页面。
- **调用模型**：Hermes 直接请求 WorkBuddy API。`w2hlink` 不转发聊天流量，不需要常驻进程，也不需要运行 WorkBuddy 客户端。

## 下载后开始

项目源码：[xxxxxeus/wb2hermesLink](https://github.com/xxxxxeus/wb2hermesLink)。
**v0.1.0 已发布**，从 [Release 页面](https://github.com/xxxxxeus/wb2hermesLink/releases/tag/v0.1.0)获取以下文件。

1. 下载 `wb2hermesLink-v0.1.0-macos.zip` 和 `SHA256SUMS`，在同目录执行 `shasum -a 256 -c SHA256SUMS`。
2. 解压，打开终端进入解压目录。无需系统 Python，启动器使用已存在的 Hermes Python。
3. 执行 `./w2hlink --help`、`./w2hlink --version`。若解压工具未保留执行权限，先 `chmod +x ./w2hlink`。
4. 指定 Hermes 安装和 profile。自动发现仅支持 `$HOME/.hermes/hermes-agent`，profile 默认 `$HERMES_HOME` 或 `$HOME/.hermes`；自定义布局请显式指定。

后续示例设置两个路径变量，不把示例值原样当作真实路径：

```bash
HERMES_ROOT="$HOME/.hermes/hermes-agent"
PROFILE="$HOME/.hermes"
./w2hlink --hermes-root "$HERMES_ROOT" --hermes-home "$PROFILE" install
```

安装会先确认范围；脚本自动化可显式传 `install --yes`。只新增 `providers.workbuddy` 和专属 `secrets.command`，不改 Hermes 全局默认、auxiliary、其它 provider 或 bitwarden。已有手工 WorkBuddy provider 或其它 command 秘密源会报冲突，不接管、不覆盖、不自动串接。

安装位置固定为 `$HERMES_HOME/integrations/w2hlink`。只复制三个公开运行文件；不会复制分发者认证。后续可用该目录里的 `w2hlink`，不再依赖下载目录。含空格/中文路径需正常 shell 引号，命令不依赖当前目录。

## 登录与复用

安装后，仅主动登录才打开浏览器：

```bash
./w2hlink --hermes-root "$HERMES_ROOT" --hermes-home "$PROFILE" login
./w2hlink --hermes-root "$HERMES_ROOT" --hermes-home "$PROFILE" status
./w2hlink --hermes-root "$HERMES_ROOT" --hermes-home "$PROFILE" check config
```

用户自己完成网页交互。取消或失败保留旧认证。成功后重启 Hermes，使 token 与关联头一起重新加载；选择 WorkBuddy 模型即可，不需要运行 WorkBuddy 客户端。

认证属于安装者，保存在安装根的 `.private/auth.json`，目录0700/文件0600，权限保护的明文文件，不是加密存储。禁止把它上传或复制进 YAML/日志。没有自动刷新、自动换号、失败后弹登录或有效期承诺。

`status`区分本地 provider、归属和快照结构/权限，不把本地就绪说成服务端仍有效。认证失效后人工重新 `login` 并重启；普通API错误不触发授权。

`secrets.command`是全profile启动秘密源，即使当时选择其它provider也可能执行；不是每条消息执行，更不是客户端代理。`override_existing:true`只覆盖helper输出的专用同名变量，其它秘密源保留。不要直接执行helper的 `env`并把输出显示到聊天或日志。

## 模型管理

新安装只带实际 API ID `hy3`、本地工作预算262144，关闭自动模型发现。ID按字面值保存，点号不拆键、不自动改大小写。容量是用户设置，不是云端最大窗口的实测结论。

```bash
./w2hlink --hermes-root "$HERMES_ROOT" --hermes-home "$PROFILE" models list
./w2hlink --hermes-root "$HERMES_ROOT" --hermes-home "$PROFILE" models add example.model-v1 --context-length 262144
./w2hlink --hermes-root "$HERMES_ROOT" --hermes-home "$PROFILE" models set example.model-v1 --context-length 524288
./w2hlink --hermes-root "$HERMES_ROOT" --hermes-home "$PROFILE" models default example.model-v1
```

`models default`只修改本provider默认，不改变 Hermes 全局默认。删除时执行 `models remove ID`；provider默认项或仍被配置引用的模型不能删除。容量至少64000，使用用户确认的值。

重复安装会核对收据和文件；保留已有模型修改，不重新灌入默认列表。手工编辑后发现漂移会拒绝覆盖，先检查并解决差异。不要手工删除收据来强行接管。

## 一次真实检查

```bash
./w2hlink --hermes-root "$HERMES_ROOT" --hermes-home "$PROFILE" check model --model hy3
```

这条命令会向模型服务发送一次短文本，可能产生上游费用。使用原生 Hermes 路由与客户端、已有机器秘密源，不创建带历史/工具的Agent，不自动登录。SDK重试与重定向关闭，有限超时；只有正常结束且有最终文字才成功。输出仅结果布尔、模型名或固定错误类别，不输出原始响应/认证。

`check config`与`check model`也可检查已有手工安装：要求相同helper版本及严格专用命令形状，不要求安装收据，不自动接管。无法验证的命令源会被拒绝，而不是执行任意共享脚本。

模型检查仅执行已核验的本工具command源，接收其四个专用变量，不加载bitwarden等其它秘密源。使用原生named-custom配置与显式认证构造客户端，不通过通用credential-pool解析器读取或回写其它auth store。有收据时配置检查还核验归属、运行文件hash和配置漂移；pending事务未恢复时拒绝就绪判定，检查本身不执行恢复。

## 停用与卸载

切换其它provider只是停用会话，不会停掉启动秘密源。卸载前先让默认模型、别名、fallback等不再引用WorkBuddy；本工具不会自动改它们或停止进程。

```bash
./w2hlink --hermes-root "$HERMES_ROOT" --hermes-home "$PROFILE" uninstall
```

仅移除本工具拥有且未漂移的两块配置和三个运行文件，保留其它设置、bitwarden和认证。不递归删除整个安装目录，不提供purge。移除配置后重启 Hermes，旧进程的内存认证才随退出清除。

若确定要删除本地认证，需另行明确处理保留的私有文件；本地删除不等于服务端撤销。手工安装缺少收据时拒绝自动卸载，应按实际归属人工处理。

## 冲突与中断

局部receipt只记录拥有的非敏感配置项、引用及运行文件hash，不备份整份配置或秘密。配置先严格解析；坏YAML、重复键、符号链接、外部修改和命令源冲突都拒绝覆盖。

受管安装、profile受管标记或原生受管目录存在时，本工具拒绝管理及就绪检查，不读取管理员策略内容，也不覆盖managed环境或隐藏overlay。即使受管目录只包含其它设置也保守拒绝；需管理员处理。

操作前写局部pending日志；正常失败回滚局部项，中断后下次管理操作识别并恢复。保留无关设置。若收到锁冲突，先确认没有其它操作仍在运行；异常终止遗留的 `$HERMES_HOME/.w2hlink.lock`才可人工移除后重试。不能据此强行清除别人的锁或漂移内容。

无法与不遵守同一锁的外部编辑器形成操作系统级多文件事务；保存前复核文件摘要，发现差异立即停止。提交后再发生的人工变化按漂移处理，不自动吞掉。

## 已知边界

- WorkBuddy原生辅助路径可能缺少额外认证头；本产品不改auxiliary，不把WB头注入其它provider全局配置。
- 显式context避免已知错误的Copilot容量推断；`discover_models:false`不意味着所有原生元数据用途都消失。
- 历史主对话/工具证据不等于所有工具、模型容量或思考效果都已验证。v0.1.0已通过33项独立离线回归（含ZIP解压后完整流程）；独立验收还从ZIP入口复用已有认证发出一次hy3请求，获得非空最终回复并正常结束。没有重新网页登录，未验证所有账号、模型容量或其它Hermes版本。
- 本仓库不包含个人配置、设计材料、历史账号记录或发布者认证。

## 开发与发行

```bash
W2HLINK_TEST_HERMES_ROOT="$HERMES_ROOT" "$HERMES_ROOT/venv/bin/python" -B -m unittest discover -s tests -q
python3 -B scripts/build_release.py
```

无 Hermes 的CI只跑标准库测试，并明确跳过原生配置/事务组，不安装依赖或访问账号。构建器先验证公开白名单及隐私，再生成可重复ZIP与SHA256SUMS；不自动创建GitHub仓库或发布。
