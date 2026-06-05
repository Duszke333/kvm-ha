# VM template image

## Get alpine image

```shell
wget https://dl-cdn.alpinelinux.org/alpine/v3.23/releases/x86_64/alpine-virt-3.23.4-x86_64.iso -O images/alpine-base.iso
```

## Create virtual disk

```shell
qemu-img create -f qcow2 images/alpine-base.qcow2 2G
```

## Install alpine base

```shell
virt-install \
    --name alpine-base \
    --ram 2048 \
    --vcpus 1 \
    --disk images/alpine-base.qcow2 \
    --os-variant alpinelinux3.23 \
    --network default \
    --graphics vnc \
    --cdrom images/alpine-base.iso
```
