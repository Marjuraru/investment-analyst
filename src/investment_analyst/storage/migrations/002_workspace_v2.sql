CREATE TABLE IF NOT EXISTS workspace_v2_metadata (
    metadata_key VARCHAR PRIMARY KEY,
    metadata_value VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id VARCHAR PRIMARY KEY,
    symbol VARCHAR NOT NULL,
    asset_class VARCHAR NOT NULL,
    quote_currency VARCHAR NOT NULL,
    is_active BOOLEAN NOT NULL,
    document_json VARCHAR NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_definitions (
    source_id VARCHAR PRIMARY KEY,
    provider_name VARCHAR NOT NULL,
    dataset_name VARCHAR NOT NULL,
    source_type VARCHAR NOT NULL,
    is_official BOOLEAN NOT NULL,
    document_json VARCHAR NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS metric_definitions (
    metric_key VARCHAR PRIMARY KEY,
    display_name VARCHAR NOT NULL,
    category VARCHAR NOT NULL,
    definition_version VARCHAR NOT NULL,
    document_json VARCHAR NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workspace_raw_json_projections_v2 (
    record_id VARCHAR NOT NULL,
    field_name VARCHAR NOT NULL,
    field_value VARCHAR NOT NULL,
    PRIMARY KEY (record_id, field_name)
);

CREATE INDEX IF NOT EXISTS workspace_raw_json_projections_v2_lookup
    ON workspace_raw_json_projections_v2 (field_name, field_value, record_id);

CREATE TABLE IF NOT EXISTS workspace_analytical_content_v2 (
    content_id VARCHAR PRIMARY KEY,
    content_kind VARCHAR NOT NULL,
    sha256 VARCHAR NOT NULL,
    byte_length BIGINT NOT NULL,
    value_bytes BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_analytical_sequences_v2 (
    sequence_id VARCHAR PRIMARY KEY,
    link_type VARCHAR NOT NULL,
    member_count BIGINT NOT NULL,
    checksum_sha256 VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_analytical_segments_v2 (
    segment_id VARCHAR PRIMARY KEY,
    link_type VARCHAR NOT NULL,
    member_count INTEGER NOT NULL,
    checksum_sha256 VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_analytical_sequence_segments_v2 (
    sequence_id VARCHAR NOT NULL,
    segment_position INTEGER NOT NULL,
    segment_id VARCHAR NOT NULL,
    PRIMARY KEY (sequence_id, segment_position)
);

CREATE TABLE IF NOT EXISTS workspace_analytical_segment_members_v2 (
    segment_id VARCHAR NOT NULL,
    member_position INTEGER NOT NULL,
    member_id VARCHAR NOT NULL,
    PRIMARY KEY (segment_id, member_position)
);

CREATE TABLE IF NOT EXISTS workspace_metric_results_v2 (
    result_id VARCHAR PRIMARY KEY,
    origin VARCHAR NOT NULL CHECK (origin IN ('HISTORICAL', 'LIVE')),
    asset_id VARCHAR NOT NULL,
    metric_key VARCHAR NOT NULL,
    value_text VARCHAR NOT NULL,
    unit VARCHAR NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL,
    parameters_content_id VARCHAR NOT NULL,
    observation_sequence_id VARCHAR NOT NULL,
    metric_sequence_id VARCHAR NOT NULL,
    algorithm_version VARCHAR NOT NULL,
    quality VARCHAR NOT NULL,
    id_version INTEGER NOT NULL,
    checksum_sha256 VARCHAR NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workspace_diagnostic_results_v2 (
    diagnostic_id VARCHAR PRIMARY KEY,
    origin VARCHAR NOT NULL CHECK (origin IN ('HISTORICAL', 'LIVE')),
    asset_id VARCHAR NOT NULL,
    mode VARCHAR NOT NULL,
    verdict VARCHAR NOT NULL,
    final_score_text VARCHAR NOT NULL,
    confidence_text VARCHAR NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL,
    algorithm_version VARCHAR NOT NULL,
    summary_content_id VARCHAR NOT NULL,
    quality VARCHAR NOT NULL,
    component_count INTEGER NOT NULL,
    evidence_count INTEGER NOT NULL,
    checksum_sha256 VARCHAR NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workspace_analytical_components_v2 (
    diagnostic_id VARCHAR NOT NULL,
    position INTEGER NOT NULL,
    component_key VARCHAR NOT NULL,
    score_text VARCHAR NOT NULL,
    weight_text VARCHAR NOT NULL,
    weighted_contribution_text VARCHAR NOT NULL,
    metric_sequence_id VARCHAR NOT NULL,
    explanation_content_id VARCHAR NOT NULL,
    PRIMARY KEY (diagnostic_id, position)
);

CREATE TABLE IF NOT EXISTS workspace_analytical_evidence_v2 (
    diagnostic_id VARCHAR NOT NULL,
    position INTEGER NOT NULL,
    metric_result_id VARCHAR NOT NULL,
    direction VARCHAR NOT NULL,
    contribution_text VARCHAR NOT NULL,
    reason_content_id VARCHAR NOT NULL,
    PRIMARY KEY (diagnostic_id, position)
);

CREATE TABLE IF NOT EXISTS workspace_v2_historical_seal (
    seal_key VARCHAR PRIMARY KEY CHECK (seal_key = 'historical-analytical-v1'),
    source_fingerprint VARCHAR NOT NULL,
    metric_count BIGINT NOT NULL,
    metric_digest VARCHAR NOT NULL,
    diagnostic_count BIGINT NOT NULL,
    diagnostic_digest VARCHAR NOT NULL,
    sealed_at TIMESTAMPTZ NOT NULL
);
