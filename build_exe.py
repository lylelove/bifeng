"""用 Nuitka 把游戏打包成单个 exe，并用 UPX 压缩。

用法：
    python build_exe.py
产物：dist/bifeng.exe （真正经 UPX 压缩的单文件 exe）

要点：
- Nuitka 把 Python 编译成 C 再链接成普通 PE（本机用 zig 编译，不带 GUARD_CF），
  所以 UPX 能正常压缩；而 PyInstaller 的引导器带 CFG，UPX 压了会「参数错误」。
- 必须加 --onefile-no-compression：否则 Nuitka 会先把自己压缩一遍，
  UPX 看到已压缩的数据会报 NotCompressibleException 拒绝打包。
- --assume-yes-for-downloads 首次会自动下载 Dependency Walker（之后有缓存）。
- 本机需有 C 编译器（这里用的 Nuitka 自带的 zig，无需额外安装 MSVC）。

如不想用 UPX，可去掉最后的 UPX 步骤，直接交付未压缩的 Nuitka onefile。
"""
import subprocess
import sys

UPX = r"D:\upx\upx.exe"
OUT = r"dist\bifeng.exe"


def main() -> None:
    subprocess.run(
        [sys.executable, "-m", "nuitka",
         "--onefile",
         "--onefile-no-compression",
         "--windows-console-mode=disable",
         "--enable-plugin=pyside6",
         "--include-package=rts",
         "--output-filename=" + OUT,
         "--assume-yes-for-downloads",
         "main.py"],
        check=True,
    )
    subprocess.run([UPX, "-9", OUT], check=True)
    print("完成：", OUT)


if __name__ == "__main__":
    main()
