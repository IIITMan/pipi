# 推送到 GitHub 指引

仓库已经在本机初始化好并完成了首次提交（100 个文件，commit `61c278f`）。
下面是把这个仓库推到 `github.com/<你的用户名>/pipi` 的步骤。

---

## 一、先在 GitHub 上建仓库

浏览器打开 https://github.com/new

| 字段 | 填什么 |
|---|---|
| Repository name | `pipi` |
| Description | 皮皮 —— 情感陪伴机器人软件系统（情感对话/稀疏记忆/人格设定/视觉感知/运动预留） |
| 可见性 | **Public**（你选的） |
| Initialize this repository with | **全部不勾**（不要 README / 不要 .gitignore / 不要 license） |

> ⚠️ **关键**：不要勾选任何"初始化"选项。本地已经有提交了，
> 远程如果有初始 commit 会冲突，push 会被拒绝。

建完后 GitHub 会显示一个空仓库页面，记下你的用户名。

---

## 二、把代码拿到你本地

有两条路，选一条。

### 方式 A：已经能在本机访问这个目录（推荐）

如果你就在这台机器上，直接跳到第三步。

### 方式 B：把 `pipi.bundle` 下载到你自己的电脑

`pipi.bundle` 是一个**包含完整提交历史的单文件仓库**（186 KB）。
下载它，然后在你电脑上执行：

```bash
# 从 bundle 克隆出完整仓库（含提交历史）
git clone pipi.bundle pipi
cd pipi

# 确认历史在
git log --oneline
# 应该看到：61c278f feat: 皮皮 —— 情感陪伴机器人软件系统 v0.1
```

> 如果你只拿到了一个 zip 压缩包而不是 bundle，那就解压后
> `cd` 进去、`git init -b main && git add -A && git commit -m "初始提交"` 也一样。

---

## 三、推送到 GitHub

把 `<你的用户名>` 换成你的 GitHub 用户名：

```bash
cd pipi

# 关联远程仓库
git remote add origin https://github.com/<你的用户名>/pipi.git

# 推上去（本地分支已经是 main）
git push -u origin main
```

会让你输入用户名和密码 —— **注意：GitHub 早就不能用账号密码了**，
密码处要填 **Personal Access Token**（见下一节）。

---

## 四、生成 Personal Access Token（GitHub 已废弃密码推送）

1. 打开 https://github.com/settings/tokens
2. 点 **Generate new token** → 选 **Fine-grained token**（更安全）
3. 填：
   - Token name: `pipi-push`
   - Expiration: 30 天（够用就行）
   - Repository access: **Only select repositories** → 选 `pipi`
   - Permissions → Repository permissions → **Contents: Read and write**
4. 生成后**立刻复制**（页面关掉就看不到了）
5. 回到终端，`git push` 提示密码时，**粘贴这个 token**

> 💡 想省事可以装 GitHub CLI：`gh auth login` 走浏览器授权，
> 之后 `gh repo create pipi --public --source=. --push` 一步到位。

---

## 五、验证

```bash
git remote -v                 # 确认 remote 地址对
git log --oneline             # 确认历史在
git ls-files | wc -l          # 应该是 100
```

打开 `https://github.com/<你的用户名>/pipi`，应该能看到：

- `README.md` 自动渲染在首页
- 100 个文件
- commit message 是完整的中文说明

---

## 六、后续更新

```bash
git add -A
git commit -m "你的改动说明"
git push
```

---

## 常见问题

**Q: `! [rejected] main -> main (fetch first)`**
远程有本地没有的提交（多半是建仓库时勾了 README）。解决：

```bash
git pull --rebase origin main
git push -u origin main
```

**Q: 推送后发现自己把密钥推上去了**
立即去 https://github.com/settings/tokens **吊销那个 token**，
然后 `git rm --cached <文件>` + 提交 + 推送。
如果密钥已经进了历史，需要重写历史（`git filter-repo`）—— 越早处理越好。

**Q: 中文文件名显示成 `\344\272\214...`**
执行一次即可：
```bash
git config --global core.quotepath false
```

**Q: 想改仓库名或可见性**
在仓库页面 Settings → General 里随时可改。

---

## 本次提交包含什么

| | |
|---|---|
| 提交 hash | `61c278f` |
| 分支 | `main` |
| 文件数 | 100 |
| 代码行数 | 13,245 |
| 已排除 | `data/`（含演示用户记忆）、`__pycache__/`、`*.db`、`.env`、模型文件、人脸底片 |

`data/pipi.db` 里有演示用的用户档案和记忆，属于隐私数据，
已在 `.gitignore` 里排除，**不会**被推上去。
