"""
skcom.helper
"""

import logging
import os.path
import random
import re
import shutil
import site
import subprocess
import time
import winreg
import zipfile

import comtypes.client
from comtypes import COMError
import win32com.client
import requests
from packaging import version
from requests.exceptions import ConnectionError as RequestsConnectionError

from skcom.exception import ShellException
from skcom.exception import NetworkException
from skcom.exception import InstallationException

def win_exec(cmd, admin_priv=False):
    """
    執行程式, 取得 stdout
    """
    charset = 'cp950'

    # ~ 自動轉換家目錄
    path = os.path.expanduser(cmd[0])
    cmd[0] = path

    if admin_priv:
        # 產生隨機檔名, 用來儲存 stdout
        existed = True
        while existed:
            fsn = random.randint(0, 65535)
            stdout_path = os.path.expanduser(r'~\stdout-%04x.txt' % fsn)
            stderr_path = os.path.expanduser(r'~\stderr-%04x.txt' % fsn)
            existed = os.path.isfile(stdout_path) or os.path.isfile(stderr_path)
        try:
            open(stdout_path, 'w').close()
            open(stderr_path, 'w').close()
        except IOError:
            raise ShellException(-1, '無法產生 stdout 暫存檔')

        deep_args = pack_arglist(cmd[1:])

        # 產生底層指令與表層參數
        surface_args = [
            'Start-Process',
            '-FilePath', cmd[0],
            '-ArgumentList', deep_args,
            '-RedirectStandardOutput', stdout_path,
            '-RedirectStandardError', stderr_path,
            '-NoNewWindow'
        ]

        # 表層參數加上雙引號套入 ArgumentList
        # TODO: 底層參數的雙引號逸脫
        surface_args = ','.join(surface_args)

        # 產生表層指令
        cmd = [
            'powershell.exe', 'Start-Process',
            '-FilePath', 'powershell.exe',
            '-ArgumentList', surface_args,
            '-Verb', 'RunAs',
            '-Wait'
        ]

        stdout = ''
        skcom_ex = None
        try:
            # 取 stdout
            comp = subprocess.run(cmd, check=True, capture_output=True)
            with open(stdout_path, 'r') as stdout_file:
                stdout = stdout_file.read()
        except subprocess.CalledProcessError as ex:
            # 執行失敗, stderr 先取表層再取底層
            stderr = ex.stderr.decode(charset).strip()
            if stderr == '':
                with open(stderr_path, 'r') as stderr_file:
                    stderr = stderr_file.read()
            skcom_ex = ShellException(ex.returncode, stderr)
        except Exception:
            # 其他沒想到的狀況
            message = 'Unexpected exception (%s): %s' % (type(ex).__name__, str(ex))
            skcom_ex = ShellException(-1, message)
        finally:
            # 移除暫存檔
            os.remove(stdout_path)
            os.remove(stderr_path)
            if skcom_ex is not None:
                raise skcom_ex
    else:
        # 如果要使用 PowerShell, 含有空白字元的參數需要加上雙引號, 避免字串解析錯誤
        if cmd[0] == 'powershell.exe':
            for i in range(1, len(cmd)):
                if cmd[i].find(' ') >= 0:
                    cmd[i] = '"{}"'.format(cmd[i])

        # 執行指令
        # 如果 shell=False, 安裝 vcredist 的時候會無法提升權限而失敗
        skcom_ex = None
        try:
            comp = subprocess.run(cmd, check=True, shell=True, capture_output=True)
            stdout = comp.stdout.decode(charset)
        except subprocess.CalledProcessError as ex:
            # 執行過程發生錯誤
            skcom_ex = ShellException(ex.returncode, ex.stderr.decode(charset))
        except FileNotFoundError as ex:
            # 沒有這個程式
            message = 'No such executable (%s)' % cmd[0]
            skcom_ex = ShellException(-1, message)
        except Exception:
            # 其他沒想到的狀況
            message = 'Unexpected exception (%s): %s' % (type(ex).__name__, str(ex))
            skcom_ex = ShellException(-1, message)
        finally:
            if skcom_ex is not None:
                raise skcom_ex

    return stdout

def pack_arglist(args):
    """
    處理 Start-Process -ArgumentList 的參數內容
    """
    packed = list(map('"{}"'.format, args))
    packed = ' '.join(packed)
    packed = "'{}'".format(packed)
    return packed

def reg_read_value(node, root=winreg.HKEY_LOCAL_MACHINE):
    """
    讀取單一值
    """
    (key, name) = node.split(':')
    handle = winreg.OpenKey(root, key)
    (value, _) = winreg.QueryValueEx(handle, name)
    winreg.CloseKey(handle)
    return value

def reg_list_value(key, root=winreg.HKEY_LOCAL_MACHINE):
    """
    列舉機碼下的所有值
    """
    i = 0
    values = {}
    handle = winreg.OpenKey(root, key)

    while True:
        try:
            (vname, value, _) = winreg.EnumValue(handle, i)
            if vname == '':
                vname = '(default)'
            values[vname] = value
            i += 1
        except OSError:
            # winreg.EnumValue(handle, i) 的 i 超出範圍
            break

    winreg.CloseKey(handle)
    return values

def reg_find_value(key, value, root=winreg.HKEY_LOCAL_MACHINE):
    """
    遞迴搜尋機碼下的值, 回傳一組 tuple (所在位置, 數值)
    字串資料採用局部比對, 其餘型態採用完整比對
    """
    i = 0
    handle = winreg.OpenKey(root, key)
    vtype = type(value)

    while True:
        try:
            values = reg_list_value(key)
            for vname in values:
                # 型態不同忽略
                if not isinstance(values[vname], vtype):
                    continue

                leaf_node = key + ':' + vname
                if isinstance(value, str):
                    # 字串採用局部比對
                    if value in values[vname]:
                        return (leaf_node, values[vname])
                else:
                    # 其餘型態採用完整比對
                    if value == values[vname]:
                        return (leaf_node, values[vname])

            subkey = key + '\\' + winreg.EnumKey(handle, i)
            result = reg_find_value(subkey, value)
            if result[0] != '':
                return result

            i += 1
        except OSError:
            # winreg.EnumKey(handle, i) 的 i 超出範圍
            break

    winreg.CloseKey(handle)
    return ('', '')

def verof_vcredist():
    """
    取得 Visual C++ 2010 可轉發套件版本資訊
    注意!! 即使套件沒安裝也不可以噴例外, 否則會中斷安裝流程
    """
    try:
        node = r'SOFTWARE\WOW6432Node\Microsoft\VisualStudio\10.0\VC\VCRedist\x64:Version'
        reg_val = reg_read_value(node)
        pkg_ver = reg_val.strip('v')
        if not re.match(r'(\d+\.){3}\d+', pkg_ver):
            pkg_ver = '0.0.0.0'
    except FileNotFoundError:
        pkg_ver = '0.0.0.0'

    return version.parse(pkg_ver)

def install_vcredist():
    """
    安裝 Visual C++ 2010 x64 Redistributable 10.0.40219.325
    """
    DEBUG_MODE = False

    # 下載與安裝 (安裝程式會自動切換系統管理員身分)
    if not DEBUG_MODE:
        url = 'https://download.microsoft.com/download/1/6/5/' + \
              '165255E7-1014-4D0A-B094-B6A430A6BFFC/vcredist_x64.exe'
        # 如果下載失敗, 會觸發 NetworkException
        vcexe = download_file(url, check_dir('~/.skcom'))
    else:
        vcexe = '~/.skcom/vcredist_x64.exe'

    # 如果拒絕安裝, 會觸發 ShellException "(1) 存取被拒。"
    cmd = [vcexe, 'setup', '/passive']
    win_exec(cmd)

    # 移除安裝包, 暫不處理檔案系統例外
    if not DEBUG_MODE:
        os.remove(vcexe)

    # 版本檢查
    expected_ver = version.parse('10.0.40219.325')
    current_ver = verof_vcredist()
    if current_ver != expected_ver:
        raise InstallationException('Visual C++ 2010 可轉發套件', expected_ver)

    return current_ver

def remove_vcredist():
    """
    移除 Visual C++ 2010 x64 Redistributable  x64 - 10.0.40219.325

    其他作法與不採用原因:
      * 方法1: Get-CimInstance ... | Invoke-CimMethod ...
        * 缺點: 無法自動切換系統管理員身分
      * 方法2: Uninstall-Package ...
        * 缺點: UAC 對話方塊不會聚焦
    """

    logger = logging.getLogger('helper')
    cmd = [
        'msiexec.exe',
        '/X{1D8E6291-B0D5-35EC-8441-6616F567A0F7}',
        '/passive'
    ]
    win_exec(cmd)

def verof_skcom():
    """
    檢查群益 API 元件是否已註冊
    注意!! 即使套件沒安裝也不可以噴例外, 否則會中斷安裝流程
    """
    skcom_ver = '0.0.0.0'
    try:
        (_, dll_path) = reg_find_value(r'SOFTWARE\Classes\TypeLib', 'SKCOM.dll')
        fso = win32com.client.Dispatch('Scripting.FileSystemObject')
        skcom_ver = fso.GetFileVersion(dll_path)
    except Exception as ex:
        # TODO: 忘了什麼時候會炸掉
        pass
    return version.parse(skcom_ver)

def install_skcom(install_ver):
    """
    安裝群益 API 元件
    """
    url = 'https://www.capital.com.tw/Service2/download/api_zip/CapitalAPI_%s.zip' % install_ver

    # 建立元件目錄
    # 暫不處理檔案系統例外
    com_path = check_dir(r'~\.skcom\lib')

    # 下載
    # 如果下載失敗, 會觸發 NetworkException
    file_path = download_file(url, com_path)

    # 解壓縮
    # 只解壓縮 64-bits DLL 檔案, 其他非必要檔案不處理
    # 讀檔時需要用 CP437 編碼處理檔名, 寫檔時需要用 CP950 處理檔名
    # 暫不處理解壓縮例外
    with zipfile.ZipFile(file_path, 'r') as archive:
        for name437 in archive.namelist():
            name950 = name437.encode('cp437').decode('cp950')
            if re.search(r'元件/x64/.+\.dll$', name950):
                dest_path = r'%s\%s' % (com_path, name950.split('/')[-1])
                with archive.open(name437, 'r') as cmpf, \
                     open(dest_path, 'wb') as extf:
                    extf.write(cmpf.read())

    # 註冊元件
    # TODO: 使用者拒絕提供系統管理員權限時, 會觸發 ShellException,
    #       但是登錄檔依然能寫入成功, 原因需要再調查
    cmd = ['regsvr32', r'%s\SKCOM.dll' % com_path]
    win_exec(cmd, admin_priv=True)

    # 版本檢查
    expected_ver = version.parse(install_ver)
    current_ver = verof_skcom()
    if current_ver != expected_ver:
        raise InstallationException('群益 API COM 元件', expected_ver)

    return current_ver

def remove_skcom():
    r"""
    解除註冊與移除 skcom 元件
    注意!! 解除註冊後, 這些登錄檔的值仍然會殘留:

    * HKLM\SOFTWARE\Classes\TypeLib\{75AAD71C-8F4F-4F1F-9AEE-3D41A8C9BA5E}
    * HKLM\SOFTWARE\Classes\WOW6432Node\TypeLib\{75AAD71C-8F4F-4F1F-9AEE-3D41A8C9BA5E}
    * HKLM\SOFTWARE\WOW6432Node\Classes\TypeLib\{75AAD71C-8F4F-4F1F-9AEE-3D41A8C9BA5E}
    * HKLM\SOFTWARE\Classes\CLSID\{54FE0E28-89B6-43A7-9F07-BE988BB40299}
    * HKLM\SOFTWARE\Classes\CLSID\{72D98963-03E9-42AB-B997-BB2E5CCE78DD}
    * HKLM\SOFTWARE\Classes\CLSID\{853EC706-F437-46E2-80E0-896901A5B490}
    * HKLM\SOFTWARE\Classes\CLSID\{AC30BAB5-194A-4515-A8D3-6260749F8577}
    * HKLM\SOFTWARE\Classes\CLSID\{E3CB8A7C-896F-4828-85FC-8975E56BA2C4}
    * HKLM\SOFTWARE\Classes\CLSID\{E7BCB8BB-E1F0-4F6F-A944-2679195E5807}

    殘留值可能會導致下次安裝時誤判已註冊
    """
    com_path = os.path.expanduser(r'~\.skcom\lib')
    com_file = com_path + r'\SKCOM.dll'
    if not os.path.isfile(com_file):
        return

    logger = logging.getLogger('helper')
    logger.info('  路徑: %s', com_path)
    logger.info('  解除註冊: %s', com_file)
    cmd = ['regsvr32', '/u', com_file]
    win_exec(cmd, admin_priv=True)

    logger.info('  移除元件目錄')
    shutil.rmtree(com_path)

def has_valid_mod():
    r"""
    檢測 comtypes\gen\SKCOMLib.py 是否已生成, 如果已生成則檢查是否連結到正確的 dll 檔

    關於例外
      import comtypes.gen.SKCOMLib as sk
      ImportError: No module named 'comtypes.gen.SKCOMLib'
    原因是 COM 對應的 comtypes\gen\{UUID}.py 檔案尚未產生
      ...\site-packages\comtypes\gen\SKCOMLib.py
      ...\site-packages\comtypes\gen\_75AAD71C_8F4F_4F1F_9AEE_3D41A8C9BA5E_0_1_0.py
    如果要重現這個問題, 只要把 comtypes 移除再安裝就會出現
      pip uninstall comtypes
      pip install comtypes
    """
    result = False

    pkgdirs = site.getsitepackages()
    for pkgdir in pkgdirs:
        if pkgdir.endswith('site-packages'):
            name_mod = pkgdir + r'\comtypes\gen\SKCOMLib.py'
            # uuid_mod = pkgdir + r'\comtypes\gen\_75AAD71C_8F4F_4F1F_9AEE_3D41A8C9BA5E_0_1_0.py'
            break

    if os.path.isfile(name_mod):
        result = True
        # pylint: disable=pointless-string-statement
        '''
        dll_path = os.path.expanduser(r'~\\.skcom\\lib\\SKCOM.dll')
        import comtypes.gen.SKCOMLib as sk
        if sk.typelib_path == dll_path:
            result = True
        else:
            logger.info('移除 site-packages\\comtypes\\gen\\SKCOMLib.py, 因為連結的 DLL 不正確')
            del comtypes.gen.SKCOMLib
            del comtypes.gen._75AAD71C_8F4F_4F1F_9AEE_3D41A8C9BA5E_0_1_0
            os.remove(name_mod)
            os.remove(uuid_mod)

        TODO: 如果跑了這一段, 會導致接下來的 comtypes.client.GetModule(dll_path) 沒有作用,
              使用者會誤以為安裝完成, 在找到解決辦法之前, 先不導入這個設計
        '''

    return result

def generate_mod():
    """
    產生 COM 元件的 comtypes.gen 對應模組
    """
    logger = logging.getLogger('helper')
    logger.info(r'生成 site-packages\comtypes\gen\SKCOMLib.py')
    dll_path = os.path.expanduser(r'~\.skcom\lib\SKCOM.dll')
    comtypes.client.GetModule(dll_path)

def clean_mod():
    r"""
    清除已產生的 site-packages\comtypes\gen\*.py
    """
    pkgdirs = site.getsitepackages()
    for pkgdir in pkgdirs:
        if not pkgdir.endswith('site-packages'):
            continue

        gendir = pkgdir + r'\comtypes\gen'
        if not os.path.isdir(gendir):
            continue

        logger = logging.getLogger('helper')
        logger.info('  路徑 %s', gendir)

        for item in os.listdir(gendir):
            if item.endswith('.py'):
                logger.info('  移除 %s', item)
                os.remove(gendir + '\\' + item)

        cache_dir = gendir + '\\' + '__pycache__'
        if os.path.isdir(cache_dir):
            logger.info('  移除 __pycache__')
            shutil.rmtree(cache_dir)

def download_file(url, save_path):
    """
    使用 8K 緩衝下載檔案
    """
    abs_path = check_dir(save_path)
    file_path = r'%s\%s' % (abs_path, url.split('/')[-1])

    try:
        with requests.get(url, stream=True) as resp:
            resp.raise_for_status()
            with open(file_path, 'wb') as dlf:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        dlf.write(chunk)
                dlf.flush()
    except RequestsConnectionError:
        # 拔網路線可以測試這段
        raise NetworkException('無法下載: {}'.format(url))

    return file_path

def check_dir(usr_path):
    """
    檢查目錄, 不存在就建立目錄, 完成後回傳絕對路徑
    """
    rel_path = os.path.expanduser(usr_path)
    if not os.path.isdir(rel_path):
        os.makedirs(rel_path)
    abs_path = os.path.realpath(rel_path)
    return abs_path
