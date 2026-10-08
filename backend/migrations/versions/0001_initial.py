"""Frozen initial PostgreSQL schema."""
from alembic import op

revision="0001"
down_revision=None
branch_labels=None
depends_on=None

def upgrade():
    op.execute('\nCREATE TABLE conversations (\n\ttitle VARCHAR(200) NOT NULL, \n\tactive_request VARCHAR(36), \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n')
    op.execute('\nCREATE TABLE embedding_cache (\n\tkey VARCHAR(64) NOT NULL, \n\tvector JSON NOT NULL, \n\tPRIMARY KEY (key)\n)\n\n')
    op.execute('\nCREATE TABLE knowledge_bases (\n\tname VARCHAR(200) NOT NULL, \n\tdescription TEXT NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\tindex_config_id VARCHAR(36), \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n')
    op.execute('\nCREATE TABLE data_sources (\n\tkb_id VARCHAR(36) NOT NULL, \n\tname VARCHAR(200) NOT NULL, \n\tkind VARCHAR(20) NOT NULL, \n\tpath TEXT NOT NULL, \n\tinterval_seconds INTEGER NOT NULL, \n\tlast_scan_at TIMESTAMP WITH TIME ZONE, \n\tnext_scan_at TIMESTAMP WITH TIME ZONE, \n\tscan_complete BOOLEAN NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(kb_id) REFERENCES knowledge_bases (id)\n)\n\n')
    op.execute('CREATE INDEX ix_data_sources_kb_id ON data_sources (kb_id)')
    op.execute('\nCREATE TABLE index_configs (\n\tkb_id VARCHAR(36) NOT NULL, \n\tconfig JSON NOT NULL, \n\tfingerprint VARCHAR(64) NOT NULL, \n\tcollection VARCHAR(100) NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(kb_id) REFERENCES knowledge_bases (id), \n\tUNIQUE (collection)\n)\n\n')
    op.execute('CREATE INDEX ix_index_configs_kb_id ON index_configs (kb_id)')
    op.execute('\nCREATE TABLE jobs (\n\tkb_id VARCHAR(36) NOT NULL, \n\tkind VARCHAR(30) NOT NULL, \n\tresource VARCHAR(100) NOT NULL, \n\tpayload JSON NOT NULL, \n\tdedup_key VARCHAR(200) NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\tphase VARCHAR(50) NOT NULL, \n\tprogress INTEGER NOT NULL, \n\tcounts JSON NOT NULL, \n\terror TEXT NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\towner VARCHAR(36), \n\tfinished_at TIMESTAMP WITH TIME ZONE, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(kb_id) REFERENCES knowledge_bases (id), \n\tUNIQUE (dedup_key)\n)\n\n')
    op.execute('CREATE INDEX ix_jobs_kb_id ON jobs (kb_id)')
    op.execute('CREATE INDEX ix_jobs_resource ON jobs (resource)')
    op.execute('CREATE INDEX ix_jobs_status ON jobs (status)')
    op.execute('\nCREATE TABLE messages (\n\tconversation_id VARCHAR(36) NOT NULL, \n\trequest_key VARCHAR(100) NOT NULL, \n\tkb_id VARCHAR(36), \n\trole VARCHAR(20) NOT NULL, \n\tcontent TEXT NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\tmeta JSON NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (conversation_id, request_key, role), \n\tFOREIGN KEY(conversation_id) REFERENCES conversations (id)\n)\n\n')
    op.execute('CREATE INDEX ix_messages_conversation_id ON messages (conversation_id)')
    op.execute('\nCREATE TABLE citations (\n\tmessage_id VARCHAR(36) NOT NULL, \n\tnumber INTEGER NOT NULL, \n\tchunk_id VARCHAR(36) NOT NULL, \n\tdocument_id VARCHAR(36) NOT NULL, \n\tversion_id VARCHAR(36) NOT NULL, \n\tname TEXT NOT NULL, \n\toriginal TEXT NOT NULL, \n\tprovenance JSON NOT NULL, \n\tscore FLOAT, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(message_id) REFERENCES messages (id)\n)\n\n')
    op.execute('CREATE INDEX ix_citations_message_id ON citations (message_id)')
    op.execute('\nCREATE TABLE documents (\n\tkb_id VARCHAR(36) NOT NULL, \n\tsource_id VARCHAR(36) NOT NULL, \n\trelative_path TEXT NOT NULL, \n\tname VARCHAR(500) NOT NULL, \n\tactive_version_id VARCHAR(36), \n\tgeneration INTEGER NOT NULL, \n\tdeleted BOOLEAN NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\terror TEXT NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (source_id, relative_path), \n\tFOREIGN KEY(kb_id) REFERENCES knowledge_bases (id), \n\tFOREIGN KEY(source_id) REFERENCES data_sources (id)\n)\n\n')
    op.execute('CREATE INDEX ix_documents_kb_id ON documents (kb_id)')
    op.execute('CREATE INDEX ix_documents_source_id ON documents (source_id)')
    op.execute('\nCREATE TABLE job_items (\n\tjob_id VARCHAR(36) NOT NULL, \n\tpath TEXT NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\terror TEXT NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(job_id) REFERENCES jobs (id)\n)\n\n')
    op.execute('CREATE INDEX ix_job_items_job_id ON job_items (job_id)')
    op.execute('\nCREATE TABLE document_versions (\n\tdocument_id VARCHAR(36) NOT NULL, \n\tfile_hash VARCHAR(64) NOT NULL, \n\tprocessing_hash VARCHAR(64) NOT NULL, \n\tsnapshot_path TEXT NOT NULL, \n\tgeneration INTEGER NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\telements JSON NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(document_id) REFERENCES documents (id)\n)\n\n')
    op.execute('CREATE INDEX ix_document_versions_document_id ON document_versions (document_id)')
    op.execute('\nCREATE TABLE chunks (\n\tkb_id VARCHAR(36) NOT NULL, \n\tdocument_id VARCHAR(36) NOT NULL, \n\tversion_id VARCHAR(36) NOT NULL, \n\tordinal INTEGER NOT NULL, \n\toriginal TEXT NOT NULL, \n\tretrieval_text TEXT NOT NULL, \n\tcontent_hash VARCHAR(64) NOT NULL, \n\tprovenance JSON NOT NULL, \n\tsearch_vector TSVECTOR, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (version_id, ordinal), \n\tFOREIGN KEY(kb_id) REFERENCES knowledge_bases (id), \n\tFOREIGN KEY(document_id) REFERENCES documents (id), \n\tFOREIGN KEY(version_id) REFERENCES document_versions (id)\n)\n\n')
    op.execute('CREATE INDEX ix_chunks_document_id ON chunks (document_id)')
    op.execute('CREATE INDEX ix_chunks_kb_id ON chunks (kb_id)')
    op.execute('CREATE INDEX ix_chunks_search_vector ON chunks USING gin (search_vector)')
    op.execute('CREATE INDEX ix_chunks_version_id ON chunks (version_id)')
    op.execute('\nCREATE TABLE index_builds (\n\tindex_config_id VARCHAR(36) NOT NULL, \n\tversion_id VARCHAR(36) NOT NULL, \n\tstate VARCHAR(20) NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (index_config_id, version_id), \n\tFOREIGN KEY(index_config_id) REFERENCES index_configs (id), \n\tFOREIGN KEY(version_id) REFERENCES document_versions (id)\n)\n\n')
    op.execute('CREATE INDEX ix_index_builds_index_config_id ON index_builds (index_config_id)')
    op.execute('CREATE INDEX ix_index_builds_version_id ON index_builds (version_id)')

def downgrade():
    op.execute('DROP TABLE index_builds')
    op.execute('DROP TABLE chunks')
    op.execute('DROP TABLE document_versions')
    op.execute('DROP TABLE job_items')
    op.execute('DROP TABLE documents')
    op.execute('DROP TABLE citations')
    op.execute('DROP TABLE messages')
    op.execute('DROP TABLE jobs')
    op.execute('DROP TABLE index_configs')
    op.execute('DROP TABLE data_sources')
    op.execute('DROP TABLE knowledge_bases')
    op.execute('DROP TABLE embedding_cache')
    op.execute('DROP TABLE conversations')
