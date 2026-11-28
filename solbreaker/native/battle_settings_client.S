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
    andl $0x1fffff, %eax # Remove the battle-rule bits.
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
.ifdef direct_hangar
    # The acknowledged solo save already has the CreateRoom controller in
    # ESI. Use the lobby's ReturnToHangar signal to unwind the screens as a
    # group. Keep the response receiver alive until dispatch returns.
    call return_to_hangar
    .fill 10, 1, 0x90
.else
    # UI actions own navigation and the saved notification, after the ACK.
    pushl $1
    leal saved_name-0x400000(%ebx), %edx
    movl %edi, %ecx
    call set_bool
.endif
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
get_title: .asciz "SolbreakerBotV2!"
empty: .asciz ""
recipe_name: .asciz "localRecipe"
return_name: .asciz "localReturnLevels"
saved_name: .asciz "localSaved"
.ifdef direct_hangar
return_to_hangar:
    movl %esi, %ecx
    call 0x93e9d0
    # The stock emitter receives its controller interface at +0x148.
    leal 0x148(%esi), %ecx
    call 0x1e94a90
    movl %esi, %ecx
    jmp 0x93e4c0
.endif

.ifdef standalone_settings
# The mode picker opens the settings form on its own lobby stack. The native
# form, map chooser and header widgets are shared with training rooms. No
# training controller, room list controller or create-room event is involved.
.global settings_open
settings_open:
    pushl %ebp
    movl %esp, %ebp
    subl $32, %esp
    pushl %ebx
    pushl %esi
    pushl %edi
    call 1f
1:  popl %ebx
    subl $(1b-0x400000), %ebx
    movl %ecx, %esi
    call 0x93e9d0                          # Keep the picker alive across the push.
    xorl %eax, %eax
    movl %eax, -4(%ebp)                   # screen root, owned ref
    movl %eax, -8(%ebp)                   # content adapter, unique
    movl %eax, -12(%ebp)                  # header controller, owned ref
    movl %eax, -16(%ebp)                  # right header widget, owned ref
    movl %eax, -20(%ebp)                  # form view, unique until transferred
    movl %eax, -24(%ebp)                  # form controller, owned ref

    movl 0x110(%esi), %eax
    testl %eax, %eax
    jz settings_open_done
    leal picker_content_name-0x400000(%ebx), %edx
    pushl %edx
    pushl %eax
    call settings_child
    testl %eax, %eax
    jz settings_open_done
    movl 0x38(%eax), %eax               # GameType root also contains its buttons.
    testl %eax, %eax
    jz settings_open_done
    movl %eax, -32(%ebp)                  # picker content, borrowed from picker

    leal screen_name-0x400000(%ebx), %eax
    pushl %eax
    pushl $(screen_package_end-screen_package-1)
    leal screen_package-0x400000(%ebx), %eax
    pushl %eax
    pushl %esi
    call settings_control
    movl %eax, -4(%ebp)
    testl %eax, %eax
    jz settings_open_done
    leal header_name-0x400000(%ebx), %eax
    pushl %eax
    pushl -4(%ebp)
    call settings_child
    testl %eax, %eax
    jz settings_open_done
    movl 0x120(%esi), %edx
    pushl %edx
    pushl %eax
    leal -12(%ebp), %eax
    pushl %eax
    call 0x19142f0                       # Instantiate the header controller.
    addl $12, %esp
    cmpl $0, -12(%ebp)
    je settings_open_done
    leal content_name-0x400000(%ebx), %eax
    pushl %eax
    pushl -4(%ebp)
    call settings_child
    testl %eax, %eax
    jz settings_open_done
    movl %eax, -28(%ebp)
    leal -28(%ebp), %eax
    pushl %eax
    leal -8(%ebp), %eax
    pushl %eax
    call 0x12741d0                       # Instantiate its view adapter.
    addl $8, %esp
    cmpl $0, -8(%ebp)
    je settings_open_done

    leal right_name-0x400000(%ebx), %eax
    pushl %eax
    pushl $(right_package_end-right_package-1)
    leal right_package-0x400000(%ebx), %eax
    pushl %eax
    pushl %esi
    call settings_control
    movl %eax, -16(%ebp)
    testl %eax, %eax
    jz settings_open_done
    movl 0x120(%esi), %eax
    addl $0x14, %eax
    pushl %eax
    leal -20(%ebp), %eax
    pushl %eax
    call 0x1e6e0a0                       # Instantiate the shared form view.
    addl $8, %esp
    cmpl $0, -20(%ebp)
    je settings_open_done

    # The first 0x1b8 bytes belong to the stock controller. Append the
    # content adapter, two owned controls, and a per-instance vtable.
    pushl $0x22c
    call 0x25d667e
    addl $4, %esp
    testl %eax, %eax
    jz settings_open_done
    movl %eax, %ecx
    pushl $0                            # Create form, not edit room.
    leal -16(%ebp), %eax
    pushl %eax
    leal -12(%ebp), %eax
    pushl %eax
    leal 0x120(%esi), %eax
    pushl %eax
    pushl -8(%ebp)
    pushl -20(%ebp)                      # Constructor consumes the form view.
    movl $0, -20(%ebp)
    call 0x1e76630
    movl %eax, -24(%ebp)
    movl -8(%ebp), %edx
    movl %edx, 0x1b8(%eax)
    movl -4(%ebp), %edx
    movl %edx, 0x1bc(%eax)
    movl -32(%ebp), %edx
    movl %edx, 0x1c0(%eax)
    movl $0, -8(%ebp)
    movl $0, -4(%ebp)
    pushl %esi
    movl (%eax), %esi
    subl $4, %esi                       # Copy RTTI and all 25 virtual methods.
    leal 0x1c4(%eax), %edi
    movl $26, %ecx
    rep movsl
    popl %esi
    leal settings_destroy-0x400000(%ebx), %edx
    movl %edx, 0x1c8(%eax)
    leal 0x1c8(%eax), %edx
    movl %edx, (%eax)

    # Keep the backdrop. Hide the mode picker until the form is destroyed.
    movl -32(%ebp), %ecx
    call 0x93e9d0
    movl -32(%ebp), %ecx
    pushl $0
    call 0xd05020
    movl -24(%ebp), %eax
    pushl 0x1bc(%eax)
    movl 0x110(%esi), %ecx             # Picker is a top-level lobby screen.
    movl (%ecx), %edx
    call *0x2c(%edx)                     # UIControl::AddControl.

    movl -24(%ebp), %eax
    movl %eax, %ecx
    call 0x93e9d0                       # Transfer a separate ref to the stack.
    pushl -24(%ebp)
    movl %esi, %ecx
    movl (%esi), %eax
    call *0x44(%eax)
    movl -24(%ebp), %ecx
    call settings_get

settings_open_done:
    movl -24(%ebp), %ecx
    call settings_release_ref
    movl -20(%ebp), %ecx
    call settings_destroy_view
    movl -16(%ebp), %ecx
    call settings_release_ref
    movl -12(%ebp), %ecx
    call settings_release_ref
    movl -8(%ebp), %ecx
    call settings_destroy_view
    movl -4(%ebp), %ecx
    call settings_release_ref
    movl %esi, %ecx
    call 0x93e4c0
    popl %edi
    popl %esi
    popl %ebx
    leave
    ret

settings_release_ref:
    testl %ecx, %ecx
    jnz 0x93e4c0
    ret

settings_destroy_view:
    testl %ecx, %ecx
    jz 1f
    movl (%ecx), %eax
    pushl $1
    call *(%eax)
1:  ret

settings_destroy:
    pushl %ebx
    pushl %esi
    pushl %edi
    movl 0x1b8(%ecx), %esi
    movl 0x1bc(%ecx), %edi
    movl 0x1c0(%ecx), %ebx
    pushl 16(%esp)
    call 0x1e7e270
    pushl %eax
    movl %esi, %ecx
    call settings_destroy_view
    movl 0x38(%edi), %ecx               # The screen's current UI parent, if any.
    testl %ecx, %ecx
    jz 1f
    pushl %edi
    movl (%ecx), %eax
    call *0x30(%eax)                    # UIControl::RemoveControl.
1:  movl %edi, %ecx
    call 0x93e4c0
    movl %ebx, %ecx
    pushl $1
    call 0xd05020
    movl %ebx, %ecx
    call 0x93e4c0
    popl %eax
    popl %edi
    popl %esi
    popl %ebx
    ret $4

# Borrow a named child from the screen; only the temporary string is owned.
settings_child:
    pushl %ebp
    movl %esp, %ebp
    subl $24, %esp
    pushl %edi
    pushl 12(%ebp)
    leal -24(%ebp), %ecx
    call 0x69a7d0
    leal -24(%ebp), %eax
    pushl %eax
    movl 8(%ebp), %ecx
    call 0xcde480
    movl %eax, %edi
    leal -24(%ebp), %ecx
    call 0x69a720
    movl %edi, %eax
    popl %edi
    leave
    ret $8

# Load and retain a single DAVA control. Package and string stay temporary.
# settings_control(picker, path, length, control_name), stdcall.
settings_control:
    pushl %ebp
    movl %esp, %ebp
    subl $56, %esp
    pushl %edi
    pushl 16(%ebp)
    pushl 12(%ebp)
    leal -56(%ebp), %ecx
    call 0x99af60
    leal -56(%ebp), %eax
    pushl %eax
    leal -4(%ebp), %eax
    pushl %eax
    movl 8(%ebp), %ecx
    call 0x759510
    leal -56(%ebp), %ecx
    call 0x6aafb0
    xorl %edi, %edi
    cmpl $0, -4(%ebp)
    je 2f
    pushl 20(%ebp)
    leal -28(%ebp), %ecx
    call 0x69a7d0
    leal -28(%ebp), %eax
    pushl %eax
    movl -4(%ebp), %ecx
    call 0xce3490
    movl %eax, %edi
    testl %edi, %edi
    jz 1f
    movl %edi, %ecx
    call 0x93e9d0
1:  leal -28(%ebp), %ecx
    call 0x69a720
    movl -4(%ebp), %ecx
    call 0x93e4c0
2:  movl %edi, %eax
    popl %edi
    leave
    ret $16

screen_name: .asciz "Screen"
header_name: .asciz "Header"
content_name: .asciz "Content"
picker_content_name: .asciz "**/GameTypesScrollView"
screen_package: .asciz "~res:/UI/Screens/Lobby/Training/SolbreakerBattleSettings.yaml"
screen_package_end:
right_name: .asciz "HeaderRight"
right_package: .asciz "~res:/UI/Screens/Lobby/Training/RoomList.yaml"
right_package_end:
.endif
.section .battle_maps,"a"
.include "battle_settings_maps.inc"
