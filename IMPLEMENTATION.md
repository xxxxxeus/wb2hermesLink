# wb2hermesLink v0.1.0 实现边界

## 运行与命名

公开项目wb2hermesLink，命令w2hlink；macOS首版，不支持原生Windows。
唯一实测组合为Hermes v0.21.3 (2026.9.14)、macOS 26.3.1 arm64。其它系统/架构未验证。
launcher的help/version只需系统shell；其它命令使用指定Hermes已有Python，-I -B运行，不安装依赖。

原helper wblink.py保留授权、原子存储、机器env/status和动态client_name行为。
管理逻辑在w2hlink_cli.py；不改Hermes Core、协议、默认模型、auxiliary或其它provider。

## 配置事务

严格YAML解析先拒绝重复键、非法根类型及循环结构，不能把原生read_raw_config的空fallback当成有效配置。
用原生read_raw_config核对结构，通过原生validate_config_structure校验；
原生utils.atomic_yaml_write写入raw配置，避免save_config规范化时改动无关用户键。
读写入口先用原生get_managed_dir/is_managed及profile标记拒绝受管环境；
不修改HERMES_MANAGED_DIR，不读取受管配置内容。隔离环境设置仅在合成测试夹具中。

安装仅拥有providers.workbuddy与secrets.command两个子块，保持bitwarden及其它秘密源。
已存在手工安装或命令源冲突拒绝；新装hy3/262144，发现关闭。
局部receipt保存原值存在性、拥有项、运行文件hash及所选Hermes路径，不保存完整配置或认证值。

pending日志在写配置前落盘，仅保存这两个局部项的前后像和既有receipt。
恢复仅当当前局部项等于事务前像或后像才进行，保留并发修改的其它字段；
未知漂移拒绝覆盖。安装复制失败只清除本次已知hash匹配文件。
卸载先撤回配置，再删除精确运行文件；中断的文件清理可以继续，认证始终保留。

profile内排他锁串行化本工具操作，保存前再次核对原配置hash。
不声称与不合作的外部编辑器/突然断电具备跨文件严格ACID；遗留锁需确认无运行进程后处理。
格式化/注释可能随原生YAML序列化变化，未涉设置的语义必须保持不变。

## 认证与检查

安装根integrations/w2hlink只复制公开运行文件，不搬开发者快照。
0700目录/0600文件明文保存，原helper的owner/symlink/大小/schema保护不减。
取消登录保留已有快照，不自动授权、刷新或承诺固定有效期。

命令源全profile启动时执行；api_key和关联头均为环境引用，避免key_env回合刷新采用旧dotenv。
X-IDE-Type为Hermes，X-IDE-Name使用WBLINK_CLIENT_NAME。
helper读取当前解释器所属Hermes版本字面量，失败回退HermesAgent；
该值与本工具v0.1.0独立，不伪造IDE版本，不改产品/权限/计费字段。

check config不读取认证，核验已有receipt、配置和文件hash；pending一律拒绝，不做恢复。
无收据的手工安装仍执行严格helper校验，不能用无收据绕过pending。
check model只调用原生CommandSource.fetch一次，在最小子进程环境运行已核验的命令，
只接受四个专用变量，不hydrate整个profile或调用其它秘密源。
随后用原生named-custom配置读取核对已展开的地址、认证和头，显式api_key/api_mode调用
resolve_provider_client的custom:workbuddy分支，不调用会访问auth store的通用runtime/pool resolver。
原生辅助客户端分支未保留的provider头通过SDK with_options设置；构造后再核对实际地址、模型、token和头。
回归真实构造SDK客户端，凭据store/pool读写与其它秘密源均设失败哨兵并断言零调用；不发送网络请求。
不创建对话Agent，不加载历史/工具，SDK重试与重定向关闭，45秒SDK超时及60秒总计时。
已有手工安装可只读检查，但helper/命令形状不符时拒绝，不自动接管。
实际非空最终文字和stop才成功；错误仅固定类别，认证拒绝提示人工login。

## 回归和发布

产品原helper回归保留；原生配置测试以合成快照验证引用load/save/reload、旧dotenv风险及容量消费。
原生事务组覆盖安装/模型/卸载、坏YAML、共享命令、符号链接、漂移、中断恢复、无关配置不变。
临时profile与最小环境隔离，网络拒绝；不使用真实账号。
发行ZIP只含8份运行/文档/模板/许可证文件，固定时间、权限、顺序和存储方式。
公开源码采用明确白名单；本地设计、历史证据、私有认证、dist与临时材料排除。
CI不安装Hermes，未提供现有安装时明确跳过原生组，不把跳过说成原生通过。

v0.1.0独立验收：33项离线回归全部通过（包含从ZIP解压路径执行安装、模型管理、检查和卸载）；另从ZIP公开入口执行一次真实hy3检查，复用保存认证，获得非空最终文字且正常stop。未重新网页登录，日常配置与原helper保持不变。在线检查不是自动化测试或CI的一部分。

辅助请求丢失WorkBuddy额外头仍是框架边界，不通过全局header或代理掩盖。
context是用户工作预算，不是上游规格验证。
公开源码入口：https://github.com/xxxxxeus/wb2hermesLink 。
v0.1.0发行准备采用草稿Release，尚未正式发布，附件不是匿名公开下载；正式发行入口为
https://github.com/xxxxxeus/wb2hermesLink/releases 。
