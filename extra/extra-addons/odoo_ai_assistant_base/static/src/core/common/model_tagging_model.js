import { Record } from "@mail/core/common/record";

export class ModelTagging extends Record {
    static id = "id";
    /** @type {Object.<number, import("models").ModelTagging>} */
    static records = {};
    /** @returns {import("models").ModelTagging} */
    static get(data) {
        return super.get(data);
    }
    /** @returns {import("models").ModelTagging|import("models").ModelTagging[]} */
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
}

ModelTagging.register();
