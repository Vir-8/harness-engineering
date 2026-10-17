INSERT INTO merchants (id, name) VALUES ('acme', 'Acme Audio'), ('globex', 'Globex Home');

-- Note: both merchants have an order called A100, with DIFFERENT contents.
INSERT INTO orders (merchant_id, id, item, total_cents, status, delivered_at) VALUES
    ('acme',   'A100', 'headphones', 5900, 'delivered', now() - interval '3 days'),
    ('acme',   'A200', 'desk lamp',  3500, 'delivered', now() - interval '45 days'),
    ('acme',   'A300', 'keyboard',   8900, 'shipped',   NULL),
    ('globex', 'A100', 'blender',    12000, 'delivered', now() - interval '20 days');
