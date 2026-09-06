# Política de composición institucional Form 13F v2

`sec-13f-composition-policy-v2` formaliza la selección determínica y point-in-time de enmiendas
oficiales de Form 13F conforme a la normativa y formatos oficiales de la SEC.

## Semántica oficial SEC

La [Guía oficial de Form 13F de la SEC](https://www.sec.gov/rules-regulations/staff-guidance/division-investment-management-frequently-asked-questions/frequently-asked-questions-about-form-13f)
y las [instrucciones de Form 13F](https://www.sec.gov/files/form13f.pdf) establecen dos tipos
exclusivos de enmienda para el archivo XML (`amendmentType`):

1. **`RESTATEMENT`**: Una enmienda que vuelve a presentar la totalidad de las tenencias reportadas para el período.
   Analíticamente opera como **`replacement`** (sustitución íntegra del conjunto público anterior).
2. **`NEW HOLDINGS`**: Una enmienda que incluye únicamente tenencias que no fueron reportadas previamente para dicho período.
   Analíticamente opera como **`supplement`** (adición de filas complementarias sobre el conjunto base).

## Distinción respecto a v1

- En v1 (`sec-13f-composition-policy-v1`), se empleó contractualmente el literal `NEW HOLDINGS ENTRIES`. Dicha política
  se preserva intacta e inmutable para no alterar los pipelines históricos existentes.
- En v2 (`sec-13f-composition-policy-v2`), se exige y reconoce exclusivamente el literal oficial XML de la SEC
  (`NEW HOLDINGS`), mapeándolo a la operación analítica `supplement`. Cualquier referencia a `NEW HOLDINGS ENTRIES`
  se rechaza en v2 bajo el estado `ambiguous` y razón `unknown_amendment_type`.

## Estados y razones de composición v2

- **`original_complete`**: Cierre original válido sin enmiendas visibles al corte (`declared_original`).
- **`amended`**: Enmienda válida seleccionada (`declared_amendment_restatement` o `declared_amendment_new_holdings`).
- **`insufficient`**: Cadena incompleta o artefacto original ausente (`no_visible_artifact`, `missing_original_artifact`, `amendment_chain_incomplete`).
- **`ambiguous`**: Inconsistencia temporal, empates o tipos no soportados (`available_at_tie`, `contradictory_amendment_chain`, `unknown_amendment_type`, `amendment_number_missing`, `invalid_amendment_number`).
- **`not_evaluable`**: Totales declarados ausentes o discordantes con las filas observadas (`declared_total_missing`, `observed_total_missing`, `declared_total_mismatch`).
