param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("probe", "register", "send")]
    [string]$Operation
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8

$script:Aumid = "Marjuraru.InvestmentAnalyst"
$script:ShortcutPath = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Investment Analyst.lnk"
$script:LaunchUri = "http://127.0.0.1:8765"

function Write-Result {
    param([hashtable]$Payload)
    $json = $Payload | ConvertTo-Json -Compress -Depth 6
    [Console]::Out.WriteLine($json)
    [Console]::Out.Flush()
}

function Enable-ToastTypes {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
}

function Enable-AumidHelper {
    if ("InvestmentAnalyst.AumidHelper" -as [type]) { return }
    $source = @"
using System;
using System.Runtime.InteropServices;

namespace InvestmentAnalyst {
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PropertyKey {
        public Guid FormatId;
        public uint PropertyId;
    }

    [StructLayout(LayoutKind.Explicit, Size = 24)]
    public struct PropVariant {
        [FieldOffset(0)] public ushort VariantType;
        [FieldOffset(8)] public IntPtr PointerValue;
    }

    [ComImport]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    [Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
    public interface IPropertyStore {
        uint GetCount(out uint propertyCount);
        uint GetAt(uint propertyIndex, out PropertyKey key);
        uint GetValue(ref PropertyKey key, out PropVariant value);
        uint SetValue(ref PropertyKey key, ref PropVariant value);
        uint Commit();
    }

    public static class AumidHelper {
        private static readonly Guid StoreGuid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
        private static readonly Guid AumidFormat = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
        private const uint ReadWrite = 2;
        private const ushort VariantLpwstr = 31;

        [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
        private static extern void SHGetPropertyStoreFromParsingName(
            string path,
            IntPtr parent,
            uint flags,
            ref Guid interfaceId,
            [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);

        private static PropertyKey Key() {
            PropertyKey key = new PropertyKey();
            key.FormatId = AumidFormat;
            key.PropertyId = 5;
            return key;
        }

        public static void SetAumid(string shortcutPath, string aumid) {
            Guid interfaceId = StoreGuid;
            IPropertyStore store;
            SHGetPropertyStoreFromParsingName(shortcutPath, IntPtr.Zero, ReadWrite, ref interfaceId, out store);
            PropertyKey key = Key();
            PropVariant value = new PropVariant();
            value.VariantType = VariantLpwstr;
            value.PointerValue = Marshal.StringToCoTaskMemUni(aumid);
            try {
                store.SetValue(ref key, ref value);
                store.Commit();
            } finally {
                Marshal.FreeCoTaskMem(value.PointerValue);
                Marshal.ReleaseComObject(store);
            }
        }

        public static string GetAumid(string shortcutPath) {
            Guid interfaceId = StoreGuid;
            IPropertyStore store;
            SHGetPropertyStoreFromParsingName(shortcutPath, IntPtr.Zero, 0, ref interfaceId, out store);
            try {
                PropertyKey key = Key();
                PropVariant value;
                store.GetValue(ref key, out value);
                if (value.VariantType != VariantLpwstr || value.PointerValue == IntPtr.Zero) {
                    return null;
                }
                return Marshal.PtrToStringUni(value.PointerValue);
            } finally {
                Marshal.ReleaseComObject(store);
            }
        }
    }
}
"@
    Add-Type -TypeDefinition $source -Language CSharp
}

function Invoke-Probe {
    $result = @{
        ok = $true
        operation = "probe"
        powershell_version = $PSVersionTable.PSVersion.ToString()
        interactive = [Environment]::UserInteractive
        winrt = $false
        aumid_registered = $false
        reason = $null
    }
    try {
        Enable-ToastTypes
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($script:Aumid) | Out-Null
        $result.winrt = $true
    }
    catch {
        $result.reason = "winrt_unavailable"
    }
    if (Test-Path -LiteralPath $script:ShortcutPath) {
        try {
            Enable-AumidHelper
            $current = [InvestmentAnalyst.AumidHelper]::GetAumid($script:ShortcutPath)
            $result.aumid_registered = ($current -eq $script:Aumid)
        }
        catch {
            $result.aumid_registered = $false
        }
    }
    if ($null -eq $result.reason -and -not $result.interactive) {
        $result.reason = "session_unavailable"
    }
    if ($null -eq $result.reason -and -not $result.aumid_registered) {
        $result.reason = "registration_failed"
    }
    Write-Result $result
}

function Invoke-Register {
    try {
        $directory = Split-Path -Parent $script:ShortcutPath
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($script:ShortcutPath)
        $shortcut.TargetPath = Join-Path $env:SystemRoot "explorer.exe"
        $shortcut.Arguments = $script:LaunchUri
        $shortcut.Description = "Investment Analyst local interface"
        $shortcut.Save()
        [System.Runtime.InteropServices.Marshal]::ReleaseComObject($shortcut) | Out-Null
        [System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell) | Out-Null
        $shortcut = $null
        $shell = $null
        [GC]::Collect()
        [GC]::WaitForPendingFinalizers()
        Enable-AumidHelper
        [InvestmentAnalyst.AumidHelper]::SetAumid($script:ShortcutPath, $script:Aumid)
        $current = [InvestmentAnalyst.AumidHelper]::GetAumid($script:ShortcutPath)
        Write-Result @{
            ok = ($current -eq $script:Aumid)
            operation = "register"
            aumid = $script:Aumid
            aumid_registered = ($current -eq $script:Aumid)
            reason = $(if ($current -eq $script:Aumid) { $null } else { "registration_failed" })
        }
    }
    catch {
        Write-Result @{
            ok = $false
            operation = "register"
            aumid = $script:Aumid
            aumid_registered = $false
            reason = "registration_failed"
        }
    }
}

function Invoke-Send {
    $raw = [Console]::In.ReadToEnd()
    if ([string]::IsNullOrWhiteSpace($raw)) {
        Write-Result @{ ok = $false; operation = "send"; attempted = 0; results = @(); reason = "invalid_result" }
        return
    }
    try {
        $payload = $raw | ConvertFrom-Json
    }
    catch {
        Write-Result @{ ok = $false; operation = "send"; attempted = 0; results = @(); reason = "invalid_result" }
        return
    }
    try {
        Enable-ToastTypes
        $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($script:Aumid)
    }
    catch {
        Write-Result @{ ok = $false; operation = "send"; attempted = 0; results = @(); reason = "winrt_unavailable" }
        return
    }
    $results = @()
    $attempted = 0
    foreach ($item in @($payload.items)) {
        try {
            $document = New-Object Windows.Data.Xml.Dom.XmlDocument
            $document.LoadXml([string]$item.xml)
            $toast = New-Object Windows.UI.Notifications.ToastNotification $document
            $toast.Tag = [string]$item.tag
            $toast.Group = [string]$item.group
            $attempted = $attempted + 1
            $notifier.Show($toast)
            $results += @{ tag = [string]$item.tag; accepted = $true; reason = $null }
        }
        catch {
            $results += @{ tag = [string]$item.tag; accepted = $false; reason = "send_error" }
        }
    }
    Write-Result @{ ok = $true; operation = "send"; attempted = $attempted; results = $results }
}

switch ($Operation) {
    "probe" { Invoke-Probe }
    "register" { Invoke-Register }
    "send" { Invoke-Send }
}
