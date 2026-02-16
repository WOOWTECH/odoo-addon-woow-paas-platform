import { Record } from "@mail/core/common/record";

export class RecordTagging extends Record {
    static id = "id";
    /** @type {Object.<number, import("models").RecordTagging>} */
    static records = {};
    /** @returns {import("models").RecordTagging} */
    static get(data) {
        return super.get(data);
    }
    /** @returns {import("models").RecordTagging|import("models").RecordTagging[]} */
    static insert(data) {
        return super.insert(...arguments);
    }

    /** @type {number} */
    id;
    /** @type {string} */
    model;
    /** @type {string} */
    name;
    /** @type {string} */
    label;
    /** @type {string} */
    url;
    /** @type {string} */
    write_date;
}

RecordTagging.register();
