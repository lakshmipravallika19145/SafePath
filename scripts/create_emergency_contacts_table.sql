-- Emergency contacts table for SafeRoute
-- Run this in MySQL:
--   source scripts/create_emergency_contacts_table.sql;

CREATE TABLE IF NOT EXISTS emergency_contacts (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    contact_name VARCHAR(100),
    phone VARCHAR(15) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Example records:
-- INSERT INTO emergency_contacts (user_id, contact_name, phone) VALUES (1, 'Mom', '9876543210');
-- INSERT INTO emergency_contacts (user_id, contact_name, phone) VALUES (1, 'Friend', '9876543222');
-- INSERT INTO emergency_contacts (user_id, contact_name, phone) VALUES (1, 'Brother', '9876543333');
