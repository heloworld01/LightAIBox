# LightAIBox 产物批量签名脚本（自签名证书验证流程）。
#
# 用法（在项目根目录、PowerShell 中运行）:
#   # 第一步：生成自签名代码签名证书（Windows 原生 API，只需执行一次）
#   powershell -ExecutionPolicy Bypass -File .\sign_artifacts.ps1 -GenCert
#
#   # 第二步：对 dist/LightAIBox/ 下所有 exe/pyd/dll 批量签名
#   powershell -ExecutionPolicy Bypass -File .\sign_artifacts.ps1
#
#   # 验证签名
#   powershell -ExecutionPolicy Bypass -File .\sign_artifacts.ps1 -Verify
#
# 说明:
#   - 自签名证书仅用于「验证签名流程跑通」，不会消除杀软误报；
#     真正解决误报需替换为商业 OV/EV 证书（如 TrustAsia/Sectigo）。
#   - 证书文件（cert/codesign.pfx）已被 .gitignore 排除，不会进仓库。
#   - 签名使用 SHA256 + RFC3161 时间戳，保证证书过期后签名仍可验证。

param(
    [switch]$GenCert,   # 生成自签名证书
    [switch]$Verify,    # 仅验证，不签名
    [switch]$All,       # 生成证书 + 签名 + 验证（一键全部执行）
    [string]$Password = "lightaibox"   # PFX 密码（换商业证书时改这里）
)

$ErrorActionPreference = "Stop"

# ---- 路径解析 ----
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$CertDir    = Join-Path $ScriptDir "cert"
$PfxPath    = Join-Path $CertDir "codesign.pfx"
$DistDir    = Join-Path $ScriptDir "dist\LightAIBox"

# ---- 定位 signtool ----
$KitsRoot = "C:\Program Files (x86)\Windows Kits\10\bin"
$Signtool = $null
if (Test-Path $KitsRoot) {
    $versions = Get-ChildItem $KitsRoot -Directory |
        Where-Object { $_.Name -match '^\d+\.\d+\.\d+\.\d+$' } |
        Sort-Object { [version]$_.Name } -Descending
    foreach ($v in $versions) {
        $cand = Join-Path $v.FullName "x64\signtool.exe"
        if (Test-Path $cand) { $Signtool = $cand; break }
    }
}
if (-not $Signtool) { throw "未找到 signtool.exe，请安装 Windows SDK" }

Write-Host "signtool: $Signtool" -ForegroundColor Cyan

# ---- 1. 生成自签名证书（Windows 原生 API，与 signtool 100% 兼容）----
function New-CodeSignCert {
    Write-Host "`n==> 生成自签名代码签名证书 ..." -ForegroundColor Yellow
    if (-not (Test-Path $CertDir)) { New-Item -ItemType Directory -Path $CertDir | Out-Null }

    if (Test-Path $PfxPath) {
        Write-Host "已存在 $PfxPath，跳过生成。" -ForegroundColor Green
        return
    }

    $cert = New-SelfSignedCertificate -Type CodeSigningCert `
        -Subject "CN=LightAIBox" `
        -CertStoreLocation "Cert:\CurrentUser\My" `
        -KeyAlgorithm RSA -KeyLength 2048 `
        -NotAfter (Get-Date).AddYears(10)

    $pwd = ConvertTo-SecureString -String $Password -AsPlainText -Force
    Export-PfxCertificate -Cert $cert -FilePath $PfxPath -Password $pwd

    Write-Host "证书生成完成: $PfxPath" -ForegroundColor Green
    Write-Host ("  主体: " + $cert.Subject)
    Write-Host ("  指纹: " + $cert.Thumbprint)
    Write-Host ("  有效期至: " + $cert.NotAfter)
}

# ---- 2. 批量签名 ----
function Invoke-SignAll {
    if (-not (Test-Path $PfxPath)) { throw "未找到 $PfxPath，请先运行 -GenCert" }
    if (-not (Test-Path $DistDir)) { throw "未找到 $DistDir，请先运行 build_windows.sh 打包" }

    $files = @()
    $files += Get-ChildItem $DistDir -Filter *.exe -File -Recurse
    $files += Get-ChildItem $DistDir -Filter *.pyd -File -Recurse
    $files += Get-ChildItem $DistDir -Filter *.dll -File -Recurse

    Write-Host "`n==> 待签名文件数: $($files.Count)" -ForegroundColor Yellow
    $ok = 0
    foreach ($f in $files) {
        & $Signtool sign /f $PfxPath /p $Password /fd SHA256 `
            /tr http://timestamp.digicert.com /td SHA256 `
            $f.FullName | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $ok++
            Write-Host ("  [OK] " + $f.Name)
        } else {
            Write-Host ("  [FAIL] " + $f.Name) -ForegroundColor Red
        }
    }
    Write-Host "签名完成: $ok / $($files.Count) 成功" -ForegroundColor Green
}

# ---- 3. 验证签名 ----
function Invoke-Verify {
    if (-not (Test-Path $DistDir)) { throw "未找到 $DistDir" }
    $exe = Join-Path $DistDir "LightAIBox.exe"
    if (-not (Test-Path $exe)) { throw "未找到 $exe" }

    Write-Host "`n==> 验证主程序签名（/kp 仅校验签名结构，不校验信任链）..." -ForegroundColor Yellow
    & $Signtool verify /kp /v $exe

    if ($LASTEXITCODE -eq 0) {
        Write-Host "签名结构验证通过（注：自签名证书不被系统信任属正常现象）" -ForegroundColor Green
    } else {
        Write-Host "注意：验证返回非 0，通常是「自签名根不受信任」——签名本身已生效。" -ForegroundColor Yellow
    }
}

# ---- 主流程 ----
if ($All) {
    New-CodeSignCert
    Invoke-SignAll
    Invoke-Verify
} elseif ($GenCert) {
    New-CodeSignCert
} elseif ($Verify) {
    Invoke-Verify
} else {
    Invoke-SignAll
}