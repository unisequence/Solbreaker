# Private 11.21 adapter. Reuses the stock request writer and view parameters.
# Link .text at 0x4b1de00. battle_settings_maps.inc holds the pinned native picker identifiers.
.code32
.text
.global settings_get, settings_reply
settings_get:
    pushl %ebp
    movl %esp, %ebp
    subl $0x30, %esp
    pushl %esi
    call 0xd55dc0
    movl %eax, %ecx
    movl (%ecx), %eax
    call *0x30(%eax)
    movl %eax, %esi
    leal get_title-0x400000(%ebx), %eax
    pushl %eax
    leal -0x18(%ebp), %ecx
    call 0x69a7d0
    leal empty-0x400000(%ebx), %eax
    pushl %eax
    leal -0x30(%ebp), %ecx
    call 0x69a7d0
    pushl $0
    pushl $1
    pushl $0
    leal -0x30(%ebp), %eax
    pushl %eax
    leal -0x18(%ebp), %eax
    pushl %eax
    pushl $0x3ff
    pushl $0
    pushl $-1
    movl %esi, %ecx
    call 0x1716670
    leal -0x18(%ebp), %ecx
    call 0x69a720
    leal -0x30(%ebp), %ecx
    call 0x69a720
    popl %esi
    leave
    ret

settings_reply:
    cmpl $127, 0xc(%ebp)
    je reply_local
    cmpl $128, 0xc(%ebp)
    je reply_local
    cmpl $15, 0xc(%ebp)
    jne 0x1e92bad
    jmp 0x1e92b17
reply_local:
    pushal
    call 1f
1:  popl %ebx
    subl $(1b-0x400000), %ebx
    movl 0x178(%esi), %edi
    movl 8(%ebp), %eax
    leal recipe_name-0x400000(%ebx), %edx
    movl %edi, %ecx
    call set_int
    cmpl $128, 0xc(%ebp)
    jne reply_saved
    movl $3, %eax
    leal return_name-0x400000(%ebx), %edx
    movl %edi, %ecx
    call set_int
    movl 8(%ebp), %eax
    xorl %edx, %edx
    movl $10000, %ecx
    divl %ecx
    testl %eax, %eax
    jz reply_done
    leal map_keys-0x400000(%ebx), %ecx
map_next:
    movl (%ecx), %edx
    testl %edx, %edx
    jz reply_done
    cmpl %edx, %eax
    je map_found
    addl $8, %ecx
    jmp map_next
map_found:
    movl 4(%ecx), %edx
    addl %ebx, %edx
    subl $24, %esp
    movl %esp, %ecx
    pushl %edx
    call 0x69a7d0
    movl %esp, %eax
    pushl %eax
    leal 0x144(%esi), %ecx
    call 0x1e91cf0
    movl %esp, %ecx
    call 0x69a720
    addl $24, %esp
    jmp reply_done
reply_saved:
    # UI actions own navigation and the saved notification, after the ACK.
    pushl $1
    leal saved_name-0x400000(%ebx), %edx
    movl %edi, %ecx
    call set_bool
reply_done:
    popal
    jmp 0x1e92bcc

# ECX=view, EDX=parameter name, EAX=int. Use the stock Any<int32>
# constructor, matching UIDataParamsComponent's declared integer type.
set_int:
    pushl %ebp
    movl %esp, %ebp
    subl $48, %esp
    pushl %esi
    pushl %edi
    movl %ecx, %esi
    movl %edx, %edi
    movl %eax, -48(%ebp)
    pushl $1
    leal -48(%ebp), %eax
    pushl %eax
    leal -44(%ebp), %ecx
    call 0x6b9450
    pushl %edi
    leal -24(%ebp), %ecx
    call 0x69a7d0
    leal -44(%ebp), %eax
    pushl %eax
    leal -24(%ebp), %eax
    pushl %eax
    movl %esi, %ecx
    call 0x7346d0
    leal -24(%ebp), %ecx
    call 0x69a720
    leal -40(%ebp), %ecx
    call 0x6a4720
    popl %edi
    popl %esi
    leave
    ret

# ECX=view, EDX=parameter name, bool on the stack.
set_bool:
    pushl %ebp
    movl %esp, %ebp
    subl $44, %esp
    pushl %esi
    pushl %edi
    movl %ecx, %esi
    movl %edx, %edi
    pushl $1
    leal 8(%ebp), %eax
    pushl %eax
    leal -44(%ebp), %ecx
    call 0x717250
    pushl %edi
    leal -24(%ebp), %ecx
    call 0x69a7d0
    leal -44(%ebp), %eax
    pushl %eax
    leal -24(%ebp), %eax
    pushl %eax
    movl %esi, %ecx
    call 0x7346d0
    leal -24(%ebp), %ecx
    call 0x69a720
    leal -40(%ebp), %ecx
    call 0x6a4720
    popl %edi
    popl %esi
    leave
    ret $4
get_title: .asciz "SolbreakerBotGet"
empty: .asciz ""
recipe_name: .asciz "localRecipe"
return_name: .asciz "localReturnLevels"
saved_name: .asciz "localSaved"
.include "battle_settings_maps.inc"
