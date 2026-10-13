# Integração de autenticação de membros

## Correções aplicadas

O callback OAuth2 do bot foi separado do domínio legado `lunarapplications.shardweb.app` e passou a usar a configuração `auth` em `configs/config_api.json`:

```text
https://febre-system-auth.shardweb.app/api/auth/callback
```

O gerador de links continua enviando o `state` no formato `bot_id:guild_id`, preservando a associação do membro ao bot e ao servidor. A renovação de tokens também usa o novo serviço de auth configurado, e o backend Socket.IO realtime existente não foi redirecionado para o site web.

Foi incluído `test_auth_integration.py` com as verificações do domínio, callback, escopo e `state`.

## Configuração externa obrigatória

No Discord Developer Portal, a aplicação OAuth2 precisa ter exatamente esta Redirect URI cadastrada:

```text
https://febre-system-auth.shardweb.app/api/auth/callback
```

O `client_id` e o `client_secret` usados pelo bot precisam pertencer à aplicação OAuth2 configurada no serviço de auth. Não coloque tokens do bot ou secrets no repositório.

## Limitação observada durante a validação

Na data da correção, o domínio solicitado não disponibilizou o contrato HTTP: a página retornou `502 Bad Gateway` via proxy e as tentativas diretas retornaram `403` do Cloudflare. Por isso não foi inventado um endpoint novo de consulta de membros. O bot continua consultando a origem já implementada no projeto; para validar o fluxo ponta a ponta, o serviço deve voltar a responder e a URI acima deve estar cadastrada.

As dependências Python do bot não estão instaladas neste ambiente, então o teste runtime não foi executado aqui; a compilação dos módulos alterados passou.

## Segurança

O arquivo original contém credenciais sensíveis em configuração. Rotacione o usuário/senha do MongoDB e qualquer token que tenha sido compartilhado ou versionado antes de colocar o bot em produção.

## Execução

Instale `requirements.txt`, configure os segredos por variável de ambiente/secret manager e execute o bot normalmente. Depois teste: gerar link no Discord, concluir OAuth2, retornar ao callback e usar um comando protegido para confirmar que o membro é reconhecido.

---

Arquivos principais alterados:

- `configs/config_api.json`
- `modules/cloud/cloud_config.py`
- `modules/cloud/oauth_panel.py`
- `test_auth_integration.py`
- `configs/config_websocket.json` foi mantido no backend realtime original.

*Nota: a integração só pode ser considerada ponta a ponta quando o serviço remoto disponibilizar o endpoint e o callback estiver cadastrado.*

 caminhos/nome de arquivo foram mantidos para preservar compatibilidade com o projeto original.

*Fim.*

## Checklist de implantação

1. Confirmar disponibilidade de `https://febre-system-auth.shardweb.app`.
2. Cadastrar a Redirect URI exatamente como mostrada acima.
3. Configurar `client_id`/`client_secret` no painel do bot.
4. Reiniciar o processo do bot.
5. Testar um membro novo e um membro já verificado.
6. Confirmar no log que erros de indisponibilidade não liberam comandos protegidos.

## Resultado esperado

Quando o serviço estiver disponível, o botão `Verificar` deve enviar o usuário ao OAuth2 do Discord, retornar ao callback do serviço solicitado e permitir que o checker do bot reconheça o ID Discord verificado.

## Observação de contrato

O site foi tratado como serviço externo; nenhuma suposição sobre rotas além do callback e da rota de renovação já usada pelo projeto foi adicionada. Se o backend do site tiver nomes de endpoint diferentes, será necessário receber a documentação/contrato ou o código do serviço para adaptar o cliente com precisão.

## Arquivos gerados

A cópia compactada corrigida é produzida fora deste documento após a validação.

## Verificação local

A compilação sintática foi executada nos módulos cloud e connections. O teste de importação completo depende das dependências de `requirements.txt`, ausentes no sandbox.

## Compatibilidade

O domínio `cloud` antigo permanece na configuração porque é usado por outras funcionalidades do bot. Apenas o fluxo de autenticação e renovação de tokens foi direcionado ao campo `auth`.

## Fim da nota

Esta nota não contém tokens, client secrets ou credenciais.

## Atualização

A configuração websocket não aponta para o domínio do site de autenticação, pois o site web não deve ser confundido com o backend realtime.

## Conclusão

A correção local elimina o callback legado; a disponibilidade e o contrato do serviço remoto continuam sendo pré-requisitos externos.

## Referência de callback

`https://febre-system-auth.shardweb.app/api/auth/callback`

## Referência de state

`<ID_DO_BOT>:<ID_DO_SERVIDOR>`

## Encerramento

Use os logs do bot e do serviço remoto para confirmar o retorno OAuth2.

## Nota final

Não foi alterado o banco remoto nem feita submissão externa.

## Fim

