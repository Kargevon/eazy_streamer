/* Eazy Streamer: запускач для Windows.
 *
 * Робить одне: запускає програму, що лежить у теці поруч із ним
 * (runtime\python\pythonw.exe -m eazy_streamer). Нічого не завантажує,
 * нічого не розпаковує, у мережу не ходить, файлів не пише.
 */
#include <windows.h>
#include <strsafe.h>
#include <wchar.h>

#define LEN 1024

static int fail(const WCHAR *text)
{
    MessageBoxW(NULL, text, L"Eazy Streamer", MB_OK | MB_ICONERROR);
    return 1;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR args, int show)
{
    WCHAR dir[LEN], python[LEN], command[LEN * 2];
    STARTUPINFOW startup;
    PROCESS_INFORMATION process;
    WCHAR *slash;
    DWORD got = GetModuleFileNameW(NULL, dir, LEN);

    (void)instance; (void)previous; (void)args; (void)show;

    if (got == 0 || got >= LEN)
        return fail(L"Не вдалося визначити теку програми. Шлях до неї задовгий.");
    slash = wcsrchr(dir, L'\\');
    if (slash == NULL)
        return fail(L"Не вдалося визначити теку програми.");
    *slash = L'\0';

    if (FAILED(StringCchPrintfW(python, LEN, L"%s\\runtime\\python\\pythonw.exe", dir)))
        return fail(L"Шлях до програми задовгий. Перенесіть теку ближче до кореня диска.");
    if (GetFileAttributesW(python) == INVALID_FILE_ATTRIBUTES)
        return fail(L"Тека програми неповна: немає runtime\\python.\n\n"
                    L"Розпакуйте архів наново і запускайте EazyStreamer.exe з розпакованої теки, "
                    L"а не просто з архіву.");
    if (FAILED(StringCchPrintfW(command, LEN * 2, L"\"%s\" -m eazy_streamer", python)))
        return fail(L"Шлях до програми задовгий.");

    ZeroMemory(&startup, sizeof startup);
    startup.cb = sizeof startup;
    if (!CreateProcessW(python, command, NULL, NULL, FALSE, 0, NULL, dir, &startup, &process))
        return fail(L"Не вдалося запустити програму. Можливо, її блокує антивірус.");
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return 0;
}
