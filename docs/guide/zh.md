# AI-PMO 入门指南

> 原文：日文版和英文版为原本，其他语言均为译文。

---

## 这是什么？

一个把项目管理（PMO）工作交给 AI 处理的工具。

例如可以做到：

- 从 Teams 会议记录**自动生成会议纪要**
- 从纪要中**提取"谁、做什么、何时完成"并登记为任务**
- 对超过期限的任务**自动催办**

只需选择一个"模板"（工作的设计图）即可运行。
不需要编程知识。

---

## 适合谁使用？

- **学生** —— 一边使用，一边学习项目管理的方法
- **中小企业** —— 没有专职 PMO 也能获得成熟的工作方式
- **大型企业** —— 用模板统一各部门各自为政的做法

**全部免费。** 不是功能受限版，也不是试用版。模板和提示词同样如此。
（AI 服务的使用费需直接支付给你所选择的提供方。）

---

## 需要准备什么

| | 需要的东西 | 费用 |
|---|---|---|
| **简易方案** | 一台电脑、AI 服务的 API 密钥 | AI 使用费（按量计费，金额很小） |
| **内部方案** | Docker、16GB 以上内存、最好有 GPU | 免费（仅电费） |

> **该选哪个？**
> 想先试试就用**简易方案**。
> 如果会议内容不能发送到外部服务，请用**内部方案**。

---

## 三步开始

### 1. 安装

请按照 [INSTALL.md](../../INSTALL.md) 的步骤操作。

- **Windows** —— 双击 `AI-PMO-Setup.exe`
- **Mac / Linux** —— 在终端运行 `./scripts/install.sh`
- **Docker** —— 运行 `./scripts/install-docker.sh`

### 2. 配置

安装完成后会自动打开配置界面。
回答几个问题即可。不清楚的话按回车就是默认值。

```
1) AI 在哪里运行？        → 1（云端）
2) 请选择 AI 提供方       → 1（OpenAI）
3) 请输入 API 密钥        → 粘贴
4) 标识组织的名称         → 公司名等（小写字母）
5) 是否启用数据库功能？    → N
```

**提供方有五个可选。** 拿不定主意就选 OpenAI，
它也支持嵌入功能，一处配置就够了。

| 提供方 | 特点 |
|---|---|
| OpenAI | 拿不定主意就选它 |
| Gemini | 处理长会议记录成本低 |
| Groq | 速度快，但需要两个密钥 |
| OpenRouter | 一个密钥可试用多种模型 |
| Claude | 文笔质量高，但需要两个密钥（没有嵌入 API） |

**如何获取 API 密钥**
在所选提供方的网站上注册账号并生成密钥。
是一串很长的字符，请勿告诉他人。

- OpenAI —— https://platform.openai.com/api-keys
- Gemini —— https://aistudio.google.com

详情请见 [PROVIDERS.md](../PROVIDERS.md)。

### 3. 试运行

```bash
aipmo validate templates/examples/meeting_minutes.yaml
```

出现以下内容即表示成功：

```
OK  templates/examples/meeting_minutes.yaml  [software] ステップ 5 件
```

其中 `ステップ 5 件` 意为“5 个步骤”，工具输出为日文。

---

## 什么是模板

记录"按什么顺序做什么"的设计图。
一个模板对应一项 PMO 工作。

```yaml
name: meeting_minutes          # 名称
trigger: "event:teams:meeting_ended"   # 启动原因（不会自动运行）

steps:                         # 做什么
  - id: fetch_transcript       # ① 获取会议记录
    adapter: teams

  - id: minutes                # ② 让 AI 撰写纪要
    llm: { profile: default }

  - id: register_jira          # ③ 登记任务
    adapter: jira
```

`event:` 只是记录“因何启动”。会议结束时并不会自动运行。
用 `aipmo run` 或手机界面传入会议信息后再执行。
定时运行请用 `trigger: "schedule:..."` 和 `aipmo schedule`。

想做的事变了，换个模板就行。
**AI 的使用方式本身也会随模板改变。**

---

## 常用操作

```bash
aipmo setup       # 重新配置
aipmo validate <文件>   # 检查模板是否有误
aipmo run <文件>        # 运行
aipmo adapters    # 查看已连接的外部工具
aipmo doctor      # 确认连接是否正常
aipmo serve       # 打开手机端界面
aipmo schedule    # 开始按设定时间自动运行
```

---

## 关于安全需要知道的

**API 密钥保存在 `.env` 中**，不会写入 `config.yaml`。
配置文件会与同事共享、会提交到 Git，因此把密钥单独分开存放。

**内部数据不会外流。** 各公司的数据存放位置相互隔离，
从技术上无法访问其他公司的数据。

**不会自动公开。** 模板无法写入公开存储。可以提交候选，到此为止。

---

## 遇到问题时

**输入 `aipmo` 提示"找不到命令"**
Mac / Linux 请执行以下命令，然后重新打开终端：
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

**Windows 上双击 `.ps1` 没有任何反应**
请改为双击 `install.bat`。

**忘记输入 API 密钥**
再次运行 `aipmo setup`。

**杀毒软件拦截安装程序**
未签名的文件可能触发警告。
如有顾虑，请改用 Mac / Linux 版或 Docker 版。

更多内容请见 [INSTALL.md](../../INSTALL.md)。

---

## 接下来阅读

- [INSTALL.md](../../INSTALL.md) —— 安装详情
- [MOBILE.md](../MOBILE.md) —— 用手机使用
- [PROVIDERS.md](../PROVIDERS.md) —— 如何选择 AI 提供方
- [AGENTS.md](../AGENTS.md) —— 让 AI 自主判断
- [TEAMS.md](../TEAMS.md) —— 对接 Teams 会议记录
- [JIRA-SLACK.md](../JIRA-SLACK.md) —— 在 Jira 登记任务、在 Slack 通知
- [SCHEDULER.md](../SCHEDULER.md) —— 按设定的时间自动运行
- [AGILE.md](../AGILE.md) —— 报告冲刺进展
- [INDUSTRIES.md](../INDUSTRIES.md) —— 建筑、市场营销等行业用法
- [LICENSE](../../LICENSE) —— MIT 许可证（允许商用、修改与再分发）
- [README.md](../../README.md) —— 原理与设计（面向开发者）
- `templates/examples/` —— 模板实例
