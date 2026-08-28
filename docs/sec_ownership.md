# SEC ownership / Section 16

SEC-CORPUS-2 incorpora evidencia documental local y point-in-time para formularios SEC 3, 4 y 5,
incluidas sus enmiendas. La integración es descriptiva: no calcula señales, recomendaciones ni
acciones de trading.

Submissions sólo declara un locator. En algunos filings ese locator es una representación XSL/HTML;
no se interpreta como el documento ownership. El importador obtiene el `index.json` oficial del
mismo accession y exige exactamente un XML superior. Conserva el locator y sus bytes como un outcome
rechazado si contiene DTD/ENTITY, HTML u otro contenido incompatible. Nunca repara, sanitiza ni
parsea esa representación.

Sólo el XML crudo clasificado como aceptado puede crear una revisión documental y statements. Cada
outcome conserva accession, locator, manifest, hash, URL y disponibilidad; los datos permanecen
append-only y se consultan por `known_at`. La ausencia se informa como `missing`, no como cero.

Esta entrega no incluye 13F, búsqueda textual, UI/API, analytics ni normalización de direcciones,
firmas, CUSIP o instrumentos. Los Schedules 13D/13G viven en la vertical independiente
[`sec_beneficial_ownership.md`](sec_beneficial_ownership.md), sin reinterpretar los contratos de
Sección 16.
