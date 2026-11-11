# Pinned 11.21 GameTypeScreen::InfoPressed at VA 0x1a5da50.
# Initialize the training list, then send its ordinary typed create event.
# showMyRoom must be false because settings do not belong to a room.
# The callback receives its controller in ECX and has no stack arguments.
# All engine calls are relative. EBX computes the actual image base so this
# replacement needs none of the original callback's PE base relocations.
# Local NativeAny at EBP-0x38 and its one-element argument array at EBP-0x24
# follow TrainingRoomList's native construction/destruction sequence.
#
# Rebuild with as --32, then ld -m elf_i386 -Ttext=0x1a5da50
# --section-start=.settings_init=0x40c8a3 -e settings_direct. Extract .text
# and .settings_init separately with objcopy -O binary --only-section.
# The bytes are pinned in client_battle_settings.py.
.code32
.text
.global settings_direct
settings_direct:
    pushl %ebp
    movl %esp, %ebp
    subl $0x38, %esp
    pushl %esi
    pushl %ebx
    pushl %edi
    movl %ecx, %edi
    call 1f
1:
    popl %ebx
    subl $(1b - 0x400000), %ebx
    movl 0x120(%ecx), %ecx
    movl (%ecx), %eax
    call *0x14(%eax)
    movl %eax, %esi
    movl %esi, %ecx
    call settings_init
    movl $0, -0x10(%ebp)
    leal -0x38(%ebp), %ecx
    pushl $1
    leal -0x10(%ebp), %eax
    pushl %eax
    call 0x1e6b930
    leal -0x38(%ebp), %eax
    pushl %eax
    leal -0x24(%ebp), %ecx
    call 0x6f0be0
    leal -0x10(%ebp), %eax
    pushl %eax
    leal -0x24(%ebp), %ecx
    pushl %ecx
    leal 0x42f09a4(%ebx), %eax
    pushl %eax
    movl %esi, %ecx
    call 0x6f3750
    leal -0x24(%ebp), %eax
    leal 0x29c060(%ebx), %ecx
    pushl %ecx
    pushl $1
    pushl $0x14
    pushl %eax
    call 0x25d6a18
    leal -0x34(%ebp), %ecx
    call 0x6a4720
    movl %edi, %ecx
    call 0x4b1de00
    popl %edi
    popl %ebx
    popl %esi
    movl %ebp, %esp
    popl %ebp
    ret

# Link this section at 0x40c8a3, after the 25-byte save-reply trampoline.
.section .settings_init,"ax"
.global settings_init
settings_init:
    movb $0, -0x10(%ebp)
    leal -0x10(%ebp), %eax
    pushl %eax
    leal 0x3d402e0(%ebx), %eax
    pushl %eax
    leal 0x42e717c(%ebx), %eax
    pushl %eax
    call 0x1a23f80
    ret
