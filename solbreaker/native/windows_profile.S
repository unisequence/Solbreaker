/* ia32 replacement for GetUserDocumentsPath in the pinned private client.
 * Return a native FilePath rooted beside client/, independent of cwd and the
 * Windows Known Folder. All absolute API references are image-base relative.
 */
.intel_syntax noprefix
.code32
.global _start
_start:
    push ebp
    mov ebp, esp
    sub esp, 0x620
    push ebx
    push esi
    push edi
    call image_base
image_base:
    pop ebx
    sub ebx, 0x5b96c0 + (image_base - _start)
    push 260
    lea eax, [ebp - 0x220]
    push eax
    push 0
    call DWORD PTR [ebx + 0x32ef368] /* GetModuleFileNameW */
    test eax, eax
    jz failed
    cmp eax, 260
    jae failed
    lea esi, [ebp - 0x220]
    lea edi, [esi + eax * 2]
    mov edx, 2
parent_directory:
    cmp edi, esi
    jbe failed
    sub edi, 2
    cmp WORD PTR [edi], 92
    je separator
    cmp WORD PTR [edi], 47
    jne parent_directory
separator:
    dec edx
    jnz parent_directory
    add edi, 2
    lea esi, [ebx + 0x5b96c0 + (directory - _start)]
    mov ecx, 11
    cld
    rep movsw
    push 0
    push 0
    push 1024
    lea eax, [ebp - 0x620]
    push eax
    push -1
    lea eax, [ebp - 0x220]
    push eax
    push 0x80 /* WC_ERR_INVALID_CHARS */
    push 65001 /* CP_UTF8 */
    call DWORD PTR [ebx + 0x32ef2f8] /* WideCharToMultiByte */
    test eax, eax
    jz failed
    dec eax
    push eax
    lea eax, [ebp - 0x620]
    push eax
    mov ecx, DWORD PTR [ebp + 8] /* caller-owned FilePath result */
    /* The native path routine assigns into an existing std::string. The
     * hidden return slot is uninitialized, so establish its empty SSO state.
     */
    xor edx, edx
    mov DWORD PTR [ecx], edx
    mov DWORD PTR [ecx + 4], edx
    mov DWORD PTR [ecx + 8], edx
    mov DWORD PTR [ecx + 12], edx
    mov DWORD PTR [ecx + 16], edx
    mov DWORD PTR [ecx + 20], 15
    mov DWORD PTR [ecx + 24], -1
    lea eax, [ebx + 0x5c15c0] /* FilePath assignment from UTF-8 view */
    call eax
    mov eax, DWORD PTR [ebp + 8]
    pop edi
    pop esi
    pop ebx
    add esp, 0x620
    pop ebp
    ret /* hidden result argument is caller-cleaned */
failed:
    push 0x74
    push -1
    call DWORD PTR [ebx + 0x32ef164] /* TerminateProcess, no global-profile fallback */
    ud2
directory:
    .short 100,111,99,117,109,101,110,116,115,92,0
