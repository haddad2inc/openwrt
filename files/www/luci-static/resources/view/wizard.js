'use strict';
'require view';
'require uci';
'require form';
'require ui';

return view.extend({
    load: function() {
        return Promise.all([
            uci.load('wizard').catch(function() { return Promise.resolve(); }),
            uci.load('wireless'),
            uci.load('network'),
            uci.load('dhcp')
        ]);
    },

    render: function() {
        var m, s, o;

        if (!uci.get('wizard', 'default')) {
            uci.add('wizard', 'wizard', 'default');
        }

        m = new form.Map('wizard', _('البرمجة السريعة'),
            _('اختر وضع التشغيل المناسب لجهازك، وسيتم تكوين الإعدادات تلقائياً.'));

        s = m.section(form.NamedSection, 'default', 'wizard');
        s.addremove = false;
        s.anonymous = true;

        s.tab('basic', _('الإعدادات الأساسية'));

        o = s.taboption('basic', form.ListValue, 'operation_mode', _('وضع البرمجة'));
        o.value('ap_wds', _('Access point WDS'));
        o.value('client_wds', _('CLIENT WDS'));
        o.value('mesh', _('MESH'));
        o.value('ap_wds_mesh', _('Access point WDS + MESH'));
        o.value('ap_wds_client', _('Access point WDS + CLIENT WDS'));
        o.value('ap_wds_vlan', _('Access point WDS + VLAN'));
        o.value('ap_wds_vlan_client', _('Access point WDS + VLAN + CLIENT WDS'));
        o.value('ap_wds_vlan_mesh', _('Access point WDS + VLAN + MESH'));
        o.value('ap_prodpand', _('Access point ProdPand'));
        o.default = 'ap_wds';
        o.rmempty = false;

        o = s.taboption('basic', form.Value, 'lan_ipaddr', _('عنوان IP'));
        o.datatype = 'ip4addr';
        o.placeholder = '192.168.1.22';
        o.rmempty = false;

        o = s.taboption('basic', form.Value, 'lan_netmask', _('قناع الشبكة'));
        o.datatype = 'ip4addr';
        o.value('255.255.255.0');
        o.value('255.255.0.0');
        o.value('255.0.0.0');
        o.default = '255.255.255.0';
        o.rmempty = false;

        var vlanModes = ['ap_wds_vlan', 'ap_wds_vlan_mesh', 'ap_wds_vlan_client'];
        o = s.taboption('basic', form.Value, 'vlan_id', _('رقم VLAN'));
        o.datatype = 'range(1, 4094)';
        o.placeholder = '100';
        o.default = '100';
        o.rmempty = false;
        vlanModes.forEach(function(mode) { o.depends('operation_mode', mode); });

        var apModes = ['ap_wds', 'ap_wds_vlan', 'ap_wds_mesh', 'ap_wds_vlan_mesh',
                       'ap_wds_client', 'ap_wds_vlan_client', 'ap_prodpand'];

        o = s.taboption('basic', form.Value, 'wifi_ssid', _('اسم شبكة البث (SSID)'));
        o.datatype = 'maxlength(32)';
        o.placeholder = 'Haddad-WiFi';
        o.rmempty = false;
        o.validate = function(section_id, value) {
            if (!value || value.length === 0)
                return _('لا يمكن ترك اسم الشبكة فارغاً!');
            return true;
        };
        apModes.forEach(function(mode) { o.depends('operation_mode', mode); });

        o = s.taboption('basic', form.Value, 'wifi_key', _('كلمة مرور الواي فاي (اتركها فارغة بدون كلمة مرور)'));
        o.datatype = 'wpakey';
        o.password = true;
        o.rmempty = true;
        apModes.forEach(function(mode) { o.depends('operation_mode', mode); });

        var meshModes = ['mesh', 'ap_wds_mesh', 'ap_wds_vlan_mesh'];

        o = s.taboption('basic', form.Value, 'mesh_id', _('معرف شبكة الميش (Mesh ID)'));
        o.placeholder = 'Haddad-Mesh';
        o.rmempty = false;
        o.validate = function(section_id, value) {
            if (!value || value.length === 0)
                return _('لا يمكن ترك معرف الميش فارغاً!');
            return true;
        };
        meshModes.forEach(function(mode) { o.depends('operation_mode', mode); });

        o = s.taboption('basic', form.ListValue, 'mesh_encryption', _('نوع تشفير الميش'));
        o.value('none', _('بدون تشفير'));
        o.value('sae', _('SAE / WPA3'));
        o.value('psk2', _('WPA2-PSK'));
        o.default = 'none';
        meshModes.forEach(function(mode) { o.depends('operation_mode', mode); });

        o = s.taboption('basic', form.Value, 'mesh_key', _('كلمة مرور الميش'));
        o.password = true;
        o.datatype = 'wpakey';
        o.rmempty = true;
        o.depends('mesh_encryption', 'sae');
        o.depends('mesh_encryption', 'psk2');
        o.validate = function(section_id, value) {
            var enc = this.map.lookupOption('mesh_encryption', section_id)[0].formvalue(section_id);
            if (enc !== 'none') {
                if (!value || value.length < 8) {
                    return _('كلمة مرور الميش مطلوبة ويجب أن تكون 8 أحرف على الأقل!');
                }
            }
            return true;
        };

        var clientModes = ['client_wds', 'ap_wds_client', 'ap_wds_vlan_client'];

        o = s.taboption('basic', form.Value, 'client_ssid', _('اسم الشبكة المستهدفة (SSID)'));
        o.placeholder = 'Target-Network';
        o.rmempty = false;
        o.validate = function(section_id, value) {
            if (!value || value.length === 0)
                return _('لا يمكن ترك اسم الشبكة المستهدفة فارغاً!');
            return true;
        };
        clientModes.forEach(function(mode) { o.depends('operation_mode', mode); });

        o = s.taboption('basic', form.ListValue, 'client_encryption', _('نوع تشفير الشبكة المستهدفة'));
        o.value('none', _('بدون تشفير'));
        o.value('psk2', _('WPA2-PSK'));
        o.value('sae', _('SAE / WPA3'));
        o.default = 'none';
        clientModes.forEach(function(mode) { o.depends('operation_mode', mode); });

        o = s.taboption('basic', form.Value, 'client_key', _('كلمة مرور الشبكة المستهدفة'));
        o.password = true;
        o.datatype = 'wpakey';
        o.rmempty = true;
        o.depends('client_encryption', 'psk2');
        o.depends('client_encryption', 'sae');
        o.validate = function(section_id, value) {
            var enc = this.map.lookupOption('client_encryption', section_id)[0].formvalue(section_id);
            if (enc !== 'none') {
                if (!value || value.length < 8) {
                    return _('كلمة مرور الشبكة المستهدفة مطلوبة ويجب أن تكون 8 أحرف على الأقل!');
                }
            }
            return true;
        };

        s.tab('advanced', _('إعدادات متقدمة'));

        o = s.taboption('advanced', form.ListValue, 'cell_density', _('طاقة البث'));
        o.value('0', _('الافتراضية'));
        o.value('1', _('طبيعية (-2dBm)'));
        o.value('2', _('متوسطة (-4dBm)'));
        o.value('3', _('عالية (-6dBm)'));
        o.default = '0';

        o = s.taboption('advanced', form.ListValue, 'channel_2g', _('قناة 2.4GHz'));
        o.value('auto', _('تلقائي'));
        for (var i = 1; i <= 11; i++) {
            o.value(String(i), _('قناة ') + i);
        }
        o.default = 'auto';

        o = s.taboption('advanced', form.ListValue, 'channel_5g', _('قناة 5GHz'));
        o.value('auto', _('تلقائي'));
        var ch5List = [36,40,44,48,52,56,60,64,100,104,108,112,116,120,124,128,132,136,140,144,149,153,157,161,165,169,173,177];
        ch5List.forEach(function(ch) {
            o.value(String(ch), _('قناة ') + ch);
        });
        o.default = '36';

        // ─── Watchcat ───
        o = s.taboption('advanced', form.Flag, 'enable_watchcat', _('تفعيل مراقب الاتصال بالانترنت (watchcat)'));
        o.rmempty = true;
        o.default = false;

        // ─── إلغاء زر الريسيت ───
        o = s.taboption('advanced', form.Flag, 'disable_reset_button', _('إلغاء عمل زر الريسيت للراوتر'));
        o.rmempty = true;
        o.default = false;

        o = s.taboption('advanced', form.Value, 'root_password', _('تغيير كلمة مرور الروت (اتركها فارغة إذا لم ترغب في التغيير)'));
        o.password = true;
        o.rmempty = true;

        o = s.taboption('advanced', form.Flag, 'remove_old_config', _('حذف إعدادات الوايرلس والنتورك القديمة وإنشاء جديدة'));
        o.rmempty = true;
        o.default = true;

        m.handleSave = function(ev) {
            var map = this;
            return map.save().then(function() {
                return uci.save();
            }).then(function() {
                ui.addNotification(null, E('p', _('تم حفظ الإعدادات وسيتم تطبيقها تلقائياً...')), 'success');
                return map.render();
            }).catch(function(err) {
                ui.addNotification(null, E('p', _('حدث خطأ أثناء الحفظ: ') + String(err)), 'error');
                throw err;
            });
        };

        m.handleApply = function(ev) {
            return this.handleSave(ev);
        };

        return m.render();
    }
});
