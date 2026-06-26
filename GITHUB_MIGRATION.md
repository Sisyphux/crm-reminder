# 迁移到 GitHub 步骤

当前项目存储在 iCloud Drive 上，.git 目录是 iCloud 的重解析点，git 命令行无法正常访问。

## 步骤

### 1. 复制项目到非 iCloud 位置

```bash
# 在桌面创建一个项目文件夹
xcopy /E /H "D:\iCloudDrive\工作\客户跟进提醒系统" "C:\Users\罗歆\Desktop\crm-reminder"
```

### 2. 在新目录初始化 Git

```bash
cd C:\Users\罗歆\Desktop\crm-reminder

# 如果旧 .git 目录被复制过来了，先删除再重新 init
rmdir /S .git
git init
git branch -m main

# 添加所有文件
git add .
git commit -m "初始化 CRM 跟进提醒系统"
```

### 3. 推送到 GitHub

```bash
# 添加远程仓库（替换为你的仓库地址）
git remote add origin https://github.com/Sisyphux/crm-reminder.git

# 推送到 GitHub
git push -u origin main
```

### 4. 后续开发

- 在 `C:\Users\罗歆\Desktop\crm-reminder` 中开发
- 每次改动后 `git add . && git commit -m "描述" && git push`
- 其他电脑只需 `git clone https://github.com/Sisyphux/crm-reminder.git`
- 数据库文件（`data/*.db`）在 .gitignore 中已排除，不会上传

### 运行项目

```bash
cd C:\Users\罗歆\Desktop\crm-reminder
python venv\Scripts\python.exe app.py
```
