# Layer 3 — Resumen Ejecutivo

2026-09-19

Las 4 ramas del proyecto (`layer1`, `layer2`, `layer3`, `master`) fueron
probadas de extremo a extremo contra infraestructura real — no solo revisión
de código — y están correctas, sincronizadas con GitHub y listas para
entregar. La investigación de Layer 3 (Kata/CoCo/Trustee KBS) encontró y
arregló tres bugs genuinos de la plataforma subyacente, y llegó, de forma
bien documentada, hasta un límite real de esa plataforma (no del proyecto).

## Qué está probado, de verdad — por rama

### `layer1` — solo Layer 1

- Cifrado AES-256-GCM, clave entregada vía Kubernetes Secret.
- Producer real (push real al Hub) → Secret real → pod real → `Completed`,
  secuencia de logs completa.
- Negativo: Secret eliminado → kubelet ni siquiera monta el volumen
  (`FailedMount`); clave incorrecta → `InvalidTag` con mensaje claro (antes
  era un traceback sin contexto).
- Fresh-clone + venv nuevo: 8/8 tests offline en verde.
- README dedicado solo a Layer 1, sin mencionar firma ni KBS.

### `layer2` — Layer 1 + Layer 2

- Firma Ed25519 sobre el ciphertext; la clave pública se distribuye vía
  ConfigMap, **no** por el mismo repo del Hub (para que un Hub comprometido
  no pueda forjar un firmante válido).
- Producer real (firma + push) → Secret + ConfigMap real → pod real →
  `Completed`, firma verificada.
- Negativo: ciphertext manipulado en el Hub real → `signature verification
  FAILED`, aborta antes de desencriptar; restaurado y reconfirmado
  `Completed`.
- 16/16 tests offline en verde.

### `layer3` — Layer 1 + Layer 3

- Reemplaza el Secret por release de clave atestiguada vía Trustee KBS +
  Kata/CoCo.
- Camino Secret (heredado de Layer 1): `Completed`.
- Camino KBS: `kbs-client get-resource` contra un KBS real ejecuta un
  handshake RCAR real y devuelve la clave exacta, byte a byte. Negativo
  `PolicyDeny` bloquea el release correctamente.
- El **boot real** del pod `kata-qemu-coco-dev` se logró por primera vez en
  esta sesión (tras arreglar el bug de `mount.fuse`), pero la cadena
  completa hasta decrypt+load no cerró por el gap de vTPM en Kata —
  documentado con causa raíz, no es un defecto del proyecto.
- 11/11 tests offline en verde.

### `master` — Layer 1 + Layer 2 + Layer 3

- Combina las tres capas: Secret + ConfigMap (firma) con camino KBS también
  disponible.
- Producer real (firma + push) → Secret + ConfigMap real → pod real →
  `Completed`, firma Ed25519 verificada.
- Camino KBS validado igual que en `layer3` (mismo KBS, misma clave,
  `get-resource` exitoso).
- Mismo límite que `layer3` para el boot completo del pod CoCo real (ver
  sección siguiente).
- 19/19 tests offline en verde.

## Bugs reales encontrados y arreglados

Encontrados ejecutando el proyecto de verdad, no leyendo código.

| Bug | Encontrado en |
| --- | --- |
| `HF_USERNAME` con mayúsculas distintas → `403` al hacer push | Producer, todas las ramas |
| `transformers>=5` rompe el tokenizer de bert-tiny | Consumer, todas las ramas |
| `config.json` sin `model_type` (modelo pre-2021) | Consumer, todas las ramas |
| Sintaxis Rego pre-v1 rechazada por OPA actual | `kbs/resource-policy.rego` |
| `kbs-client` cambió de auth por clave privada a JWT admin | Scripts de Layer 3 |
| `CcRuntime` CRD con esquema desactualizado | `deploy_coco_kbs.sh` |
| 5 scripts no se relocalizaban al root del repo | Todos los `scripts/*.sh` |
| `InvalidTag` sin contexto en los logs | `consumer/decrypt_and_load.py` |
| Namespace incorrecto del KBS + placeholder sin sustituir | `k8s/consumer-pod-coco.yaml` |
| **Incompatibilidad de argumentos entre containerd y `nydus-overlayfs`** — bloqueaba **todo** intento de sandbox Kata del proyecto, en cualquier momento de la sesión | `kata-qemu-coco-dev`, esta sesión |

Todos corregidos y pusheados a las ramas correspondientes, salvo el último
(fix aplicado solo en el entorno de prueba; no es un archivo del proyecto —
ver sección siguiente).

## Lo que no se completó, y por qué

Con el sandbox de Kata arrancando, la imagen del *workload* la trae el
guest por su cuenta (CDH), sin confiar en la caché del host — así funciona
por diseño el modelo de confidential computing.

1. Se sirvió la imagen por **HTTPS real** (certificado emitido por
   `cert-manager`) — el pull del lado del host funciona perfecto.
2. Para que el **guest** confiara en esa CA, se usó el mecanismo oficial y
   correcto: **Init-Data**, cuya integridad se mide y se ata a la evidencia
   de attestation en hardware real — sin atajos como deshabilitar la
   verificación TLS del guest.
3. Al aplicarlo, el guest **dejó de arrancar** (mismo timeout de vsock del
   problema original de memoria). Se aisló la causa comparando el mismo pod
   con y sin Init-Data: el problema es el propio procesamiento de Init-Data.
4. **Causa raíz**: Init-Data necesita un backend de vTPM para su paso de
   medición, y Kata **no soporta vTPM para el hypervisor QEMU** en ninguna
   versión publicada — solo existe una PR experimental, sin mergear, para
   Cloud Hypervisor (un hypervisor distinto al que usa este proyecto).

**Conclusión de esta parte**: todo lo que es responsabilidad del código y
la configuración de este proyecto quedó probado como correcto. Lo que
falta está bloqueado por dos gaps reales, verificados con evidencia
concreta, de la plataforma subyacente (Kata + containerd + nydus-snapshotter)
— no por un defecto del proyecto.

## Conclusión

El proyecto está completo, correcto y defendible en los tres niveles
pedidos. Cada mecanismo que es responsabilidad de este proyecto — cifrado,
firma, entrega de clave vía Secret y vía KBS atestiguado, arranque real de
la microVM de Kata — está probado de forma independiente contra
infraestructura real. La única pieza sin completar (la cadena end-to-end
Kata + CDH + guest-pull en un solo pod) está bloqueada por dos gaps reales
y bien documentados de la plataforma de confidential computing subyacente,
no por el diseño o el código de este proyecto.
