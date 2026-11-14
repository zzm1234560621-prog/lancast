# 部署 LanCast

LanCast 的“服务端”是运行在**要被投屏的那台 Windows 电脑**上的程序；手机/平板只打开浏览器，不装 App。它必须运行在当前登录用户的桌面会话里，才能采集屏幕和注入鼠标键盘，所以不要把它装成 Windows 服务或计划任务里的“不管用户是否登录都运行”。

## 1. 直接运行 Windows 包（推荐给普通用户）

1. 从 [GitHub Releases](https://github.com/zzm1234560621-prog/lancast/releases/latest) 下载 `LanCast-0.1.0-win64.zip`。
2. 解压到一个固定目录，例如 `C:\Apps\LanCast`。**整个文件夹要一起保留**，`LanCast.exe` 旁边的 `_internal` 不能删。
3. 双击 `LanCast.exe`。如果 SmartScreen 提示“未知发布者”，选“更多信息 → 仍要运行”。
4. Windows 防火墙第一次会询问是否允许网络访问，**勾选专用/公用网络并允许**。
5. 启动后控制台会显示：
   - 手机访问地址，例如 `https://192.168.3.16:8443`
   - 6 位配对码
   - 本机管理页地址（会自动在浏览器打开）
6. 手机连同一个 Wi-Fi，扫码或在浏览器输入上面的地址；首次会提示证书不受信任，按页面说明继续。
7. 在电脑管理页点“允许”；需要手机控制电脑时，同时打开“允许控制”。

首次启动会花约 5–9 秒做编码器自动测速，之后启动约 3–4 秒。

### 数据保存在哪里

`%LOCALAPPDATA%\LanCast\`：

- `ca.crt.pem` / `ca.key.pem` / `server.crt.pem` / `server.key.pem`：本机 CA 和服务器证书
- `devices.json`：已记住的设备（只保存令牌摘要，不保存明文令牌）
- `settings.json`：管理页改过的画质/共享设置
- `autotune.json`：自动测速结果

备份或迁移时把这个目录一起带走即可；删除它相当于恢复出厂设置（所有设备要重新配对，证书也会重签）。

## 2. 开机自动启动

推荐用**当前用户的启动文件夹**，这样程序运行在桌面会话里，屏幕采集和远程控制才可用：

1. `Win + R` 输入 `shell:startup`，回车。
2. 在打开的文件夹里新建快捷方式，目标填：

   ```text
   "C:\Apps\LanCast\LanCast.exe"
   ```

   起始位置填 `C:\Apps\LanCast`。
3. 下次登录 Windows 会自动启动，控制台会显示新的配对码和管理页地址。

注意：LanCast 每次启动会生成新的管理页 token，所以不要把带 token 的管理页地址存成固定书签；从控制台点开最稳妥。

## 3. 从源码部署

要求 Windows 10/11 64 位、Python 3.11 64 位。

```powershell
git clone https://github.com/zzm1234560621-prog/lancast.git
cd lancast
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m lancast
```

启动前可以先检查环境：

```powershell
.venv\Scripts\python -m lancast doctor
```

`doctor` 会检查显示器、DXGI 采集、H.264 编码器、端口、防火墙和证书。

## 4. 自己打包

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

输出 `packaging\dist\LanCast\LanCast.exe`（文件夹版，启动快，推荐分发整个文件夹）。

单文件版：

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -OneFile
```

输出 `packaging\dist\LanCast.exe`。单文件方便拷贝，但每次启动要先解压约 100 MB，会慢几秒。

打包后自检：

```powershell
powershell -ExecutionPolicy Bypass -File packaging\verify.ps1 `
  -Python (Resolve-Path .venv\Scripts\python.exe) `
  -Exe (Resolve-Path packaging\dist\LanCast\LanCast.exe)
```

它会用测试图案启动 exe，检查页面、配对、WebSocket 和 30 帧 H.264。

## 5. 网络与防火墙

- 手机和电脑必须在同一局域网；访客 Wi-Fi、开了 AP 隔离的路由器会互相不通。
- 建议给电脑固定 IP（路由器 DHCP 绑定），否则 IP 变化后手机要重新配对。
- 优先 5 GHz Wi-Fi；2.4 GHz 容易抖动。
- 默认端口：局域网 HTTPS/WSS **8443**，本机管理页 **8442**（只监听 127.0.0.1）。
- 如果 Windows 把当前网络标成“公用网络”，防火墙可能拦截。管理员 PowerShell：

  ```powershell
  New-NetFirewallRule -DisplayName "LanCast" -Direction Inbound -Protocol TCP `
    -LocalPort 8443 -Action Allow -Profile Private,Public -RemoteAddress LocalSubnet
  ```

  更推荐把当前 Wi-Fi 改成“专用网络”。
- 需要从 Tailscale / ZeroTier 访问时，加 `--allow-cidr 100.64.0.0/10`（按你的网段调整）。
- **不要把 8443 映射到公网**。这是给局域网用的，暴露到公网会带来不必要的攻击面。

## 6. 手机安装证书（可选）

不装也能用，只是每次首次访问会提示证书不受信任。安装后没有警告，也能防中间人：

1. 手机打开 `https://<电脑IP>:8443/ca.crt`。
2. iPhone/iPad：设置 → 通用 → VPN 与设备管理 → 安装描述文件；再到 设置 → 通用 → 关于本机 → 证书信任设置，打开完全信任。
3. Android：设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书。

只在你自己的电脑和手机上安装这个 CA。

## 7. 升级与卸载

升级：

1. 关闭正在运行的 LanCast。
2. 用新版本替换整个 `LanCast` 文件夹（保留 `%LOCALAPPDATA%\LanCast` 数据目录）。
3. 重新启动；设备、证书、画质设置都会保留。

卸载：

1. 关闭 LanCast。
2. 删除程序文件夹。
3. 如要彻底清理，删除 `%LOCALAPPDATA%\LanCast`、启动文件夹里的快捷方式，以及防火墙规则：

   ```powershell
   Remove-NetFirewallRule -DisplayName "LanCast"
   ```

## 8. 生产使用建议

- 保持“新设备需要我批准”开启。
- 远程控制默认关闭；只在需要时给具体设备打开“允许控制”，用完关掉。
- 管理页只在本机 `127.0.0.1` 可访问，不要通过端口转发暴露。
- 定期撤销不用的已记住设备。
- 更新后先在本机用浏览器和管理页自检，再让手机连。
- 延迟优先选 **1080p60 低延迟模式**；画质优先再选“原画/自定义”。240fps 只有在显示器、手机和编码器都支持时才有意义。

## 9. 快速排查

| 现象 | 先检查 |
|---|---|
| 手机打不开 | 同一 Wi-Fi？防火墙？IP 是否变化？管理页“其他地址” |
| 一直“正在连接” | 安装本地 CA；或换浏览器/关闭代理 |
| 黑屏/转圈 | 工具栏切“兼容 JPEG”；确认电脑没有锁屏/UAC 安全桌面 |
| 延迟高 | 手机画质切 1080p60 或 720p60；码率 10–20 Mbps |
| 有画面但不能控制 | 管理页给该设备打开“允许控制”；总开关是否开 |
| 端口冲突 | `--port 9443 --admin-port 9442` |
| 每次设置都丢 | 确认数据目录可写；设置现在会写入 `settings.json` |
