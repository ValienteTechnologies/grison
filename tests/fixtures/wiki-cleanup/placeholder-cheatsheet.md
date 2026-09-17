# SSH Access Commands

Connect with a jump host:

```
ssh <user>@<jumphost> -o ProxyCommand="ssh -W %h:%p <user>@<bastion>"
```

Or inline: `ssh <user>:<password>@<domain>` then run `nmap <target>` against <ip>.

Hash lookup: `hashcat -m 1000 <hash> wordlist.txt`
